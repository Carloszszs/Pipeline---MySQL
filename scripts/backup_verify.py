"""Verificación efímera de backups para MySQL 8.0.

Flujo:
  1. Levanta dos contenedores efímeros: ORIGEN y DESTINO (datos en tmpfs, se destruyen al final).
  2. Aplica todas las migraciones en ORIGEN y carga datos de muestra (incluye acentos/emoji y FKs).
  3. Genera un backup con mysqldump desde ORIGEN.
  4. Restaura ese backup en DESTINO.
  5. Compara ORIGEN vs DESTINO: columnas, llaves foráneas, filas y CHECKSUM TABLE por tabla.
  6. Verifica que el motor de migraciones considere la BD restaurada "al día".
  7. Prueba negativa: altera DESTINO a propósito y confirma que la comparación lo detecta.

Uso (desde la raíz del repo):
    python scripts/backup_verify.py
    python scripts/backup_verify.py --keep-dump     # guarda el .sql en backups/ (ignorado por git)

Código de salida: 0 = backup verificado, 1 = fallo, 130 = interrumpido.
Reutiliza el motor de scripts/migrate.py (no duplica lógica).
"""
import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import migrate as mig  # scripts/migrate.py (mismo directorio)

IMAGE = "mysql:8.0"
DB_NAME = "db_backup_test"
READY_TIMEOUT_S = 90
PID = os.getpid()
SRC_NAME = f"mysql_bv_origen_{PID}"
DST_NAME = f"mysql_bv_destino_{PID}"
ROOT_PASSWORD = secrets.token_urlsafe(16)
BACKUPS_DIR = Path(__file__).resolve().parent.parent / "backups"

SAMPLE_DATA_SQL = """
INSERT INTO usuarios (nombre, email, telefono) VALUES
    ('José Ñandú 🚀', 'jose@example.com',  '+52 81 5555 0001'),
    ('María Pérez',   'maria@example.com', NULL),
    ('Carlos López',  'carlos@example.com', '+52 81 5555 0003');

INSERT INTO logs_auditoria (usuario_id, accion)
    SELECT id, 'login' FROM usuarios;

INSERT INTO logs_auditoria (usuario_id, accion)
    SELECT id, 'actualizar_perfil' FROM usuarios WHERE email LIKE 'jose%';
"""


def start_container(name):
    mig.run([
        "docker", "run", "-d", "--name", name,
        "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PASSWORD}",
        "-e", f"MYSQL_DATABASE={DB_NAME}",
        "--tmpfs", "/var/lib/mysql",
        IMAGE,
        "--performance-schema=OFF",
        "--innodb-buffer-pool-size=64M",
    ])


def wait_until_ready(target):
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        state = mig.run(["docker", "inspect", "-f", "{{.State.Running}}", target.container],
                        check=False, timeout=10)
        if state.stdout.strip() != "true":
            raise RuntimeError(f"El contenedor {target.container} se detuvo durante la inicialización.")
        try:
            if target.ping():
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    raise TimeoutError(f"{target.container} no estuvo listo en {READY_TIMEOUT_S}s")


def make_dump(src, dest):
    """mysqldump desde el contenedor origen; se guarda como bytes (sin traducir saltos de línea)."""
    cmd = [
        "docker", "exec", "-e", f"MYSQL_PWD={src.password}", src.container,
        "mysqldump", "-uroot", "-h127.0.0.1", "--protocol=TCP",
        "--single-transaction", "--routines", "--triggers", "--events",
        "--set-gtid-purged=OFF", "--default-character-set=utf8mb4",
        src.database,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"mysqldump falló: {result.stderr.decode('utf-8', 'replace').strip()}")
    dest.write_bytes(result.stdout)
    if dest.stat().st_size == 0 or b"CREATE TABLE `usuarios`" not in result.stdout:
        raise AssertionError("El backup está vacío o no contiene las tablas esperadas.")
    return dest.stat().st_size


def restore_dump(dst, dump_path):
    cmd = [
        "docker", "exec", "-i", "-e", f"MYSQL_PWD={dst.password}", dst.container,
        "mysql", "-uroot", "-h127.0.0.1", "--protocol=TCP",
        "--default-character-set=utf8mb4", dst.database,
    ]
    result = subprocess.run(cmd, input=dump_path.read_bytes(), capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"La restauración falló: {result.stderr.decode('utf-8', 'replace').strip()}")


def snapshot(target):
    """Huella de la BD: estructura + filas + checksum de cada tabla."""
    columns = target.query(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )
    fks = target.query(
        "SELECT CONSTRAINT_NAME, TABLE_NAME, REFERENCED_TABLE_NAME, DELETE_RULE, UPDATE_RULE "
        "FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA = DATABASE() "
        "ORDER BY CONSTRAINT_NAME"
    )
    names = [r[0] for r in target.query(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
    )]
    tables = {}
    for name in names:
        rows = target.scalar(f"SELECT COUNT(*) FROM `{name}`")
        checksum = target.query(f"CHECKSUM TABLE `{name}`")[0][1]
        tables[name] = (rows, checksum)
    return {"columns": columns, "fks": fks, "tables": tables}


def compare(src_snap, dst_snap):
    """Devuelve la lista de diferencias (vacía = backup idéntico al origen)."""
    problems = []
    if src_snap["columns"] != dst_snap["columns"]:
        problems.append("La estructura de columnas difiere.")
    if src_snap["fks"] != dst_snap["fks"]:
        problems.append("Las llaves foráneas difieren.")
    src_t, dst_t = src_snap["tables"], dst_snap["tables"]
    for name in sorted(set(src_t) | set(dst_t)):
        if name not in dst_t:
            problems.append(f"Tabla '{name}' falta en la restauración.")
        elif name not in src_t:
            problems.append(f"Tabla '{name}' sobra en la restauración.")
        else:
            (s_rows, s_chk), (d_rows, d_chk) = src_t[name], dst_t[name]
            if s_rows != d_rows:
                problems.append(f"'{name}': filas {s_rows} (origen) vs {d_rows} (restaurado).")
            if s_chk != d_chk:
                problems.append(f"'{name}': CHECKSUM TABLE distinto ({s_chk} vs {d_chk}).")
    return problems


def dump_logs(name):
    try:
        logs = mig.run(["docker", "logs", "--tail", "20", name], check=False, timeout=15)
        print(f"--- Últimas líneas del log de {name} ---")
        print((logs.stdout + logs.stderr).strip())
    except Exception as exc:
        print(f"No se pudieron obtener los logs de {name}: {exc}")


def cleanup(names):
    print("Limpiando entorno: destruyendo contenedores efímeros...")
    for name in names:
        try:
            r = mig.run(["docker", "rm", "-f", "-v", name], check=False, timeout=60)
            if r.returncode != 0 and "No such container" not in r.stderr:
                print(f"ADVERTENCIA: no se pudo eliminar {name}: {r.stderr.strip()}")
        except Exception as exc:
            print(f"ADVERTENCIA: fallo en la limpieza de {name}: {exc}")


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verifica que un backup de MySQL se pueda restaurar íntegro.")
    parser.add_argument("--keep-dump", action="store_true", help="guardar el .sql en backups/")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _on_sigterm)
    created = []
    try:
        if shutil.which("docker") is None:
            print("Docker no está instalado o no está en el PATH.")
            return 1
        if mig.run(["docker", "info"], check=False, timeout=30).returncode != 0:
            print("El daemon de Docker no está disponible (¿Docker Desktop abierto?).")
            return 1

        print("[1/7] Creando contenedores efímeros (origen y destino)...")
        for name in (SRC_NAME, DST_NAME):
            created.append(name)
            start_container(name)
        src = mig.Target(SRC_NAME)
        dst = mig.Target(DST_NAME)

        print("[2/7] Esperando a que ambos MySQL acepten conexiones...")
        wait_until_ready(src)
        wait_until_ready(dst)

        print("[3/7] Origen: aplicando migraciones y cargando datos de muestra...")
        mig.migrate(src)
        before = src.scalar("SELECT COUNT(*) FROM usuarios")   # v1 ya inserta usuarios de prueba
        src.execute(SAMPLE_DATA_SQL)
        after = src.scalar("SELECT COUNT(*) FROM usuarios")
        if after != before + 3:
            raise AssertionError(f"Los datos de muestra no se cargaron: usuarios {before} -> {after} (esperado +3).")

        print("[4/7] Generando backup con mysqldump...")
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "backup.sql"
            size = make_dump(src, dump_path)
            print(f"    backup generado: {size:,} bytes")

            print("[5/7] Restaurando backup en el contenedor destino...")
            restore_dump(dst, dump_path)

            if args.keep_dump:
                BACKUPS_DIR.mkdir(exist_ok=True)
                saved = BACKUPS_DIR / f"backup_verify_{datetime.now():%Y%m%d_%H%M%S}.sql"
                shutil.copyfile(dump_path, saved)
                print(f"    backup guardado en: {saved}")

        print("[6/7] Comparando origen vs restaurado (estructura, filas, checksums)...")
        src_snap, dst_snap = snapshot(src), snapshot(dst)
        problems = compare(src_snap, dst_snap)
        if problems:
            raise AssertionError("El backup NO es fiel al origen:\n    - " + "\n    - ".join(problems))
        for name, (rows, chk) in src_snap["tables"].items():
            print(f"    OK {name}: {rows} filas, checksum {chk}")
        if mig.migrate(dst) != []:
            raise AssertionError("La BD restaurada tiene migraciones pendientes: el backup está incompleto.")
        print("    OK motor de migraciones: la BD restaurada está al día.")

        print("[7/7] Prueba negativa: alterar el destino debe ser detectado...")
        dst.execute("DELETE FROM logs_auditoria LIMIT 1;")
        if not compare(src_snap, snapshot(dst)):
            raise AssertionError("El verificador NO detectó una alteración: no es confiable.")
        print("    OK la alteración fue detectada.")

        print("Verificación de backup completada con éxito.")
        return 0
    except KeyboardInterrupt:
        print("Interrumpido por el usuario o por el sistema.")
        return 130
    except Exception as exc:
        print(f"Error durante el proceso: {exc}")
        for name in created:
            dump_logs(name)
        return 1
    finally:
        if created:
            cleanup(created)


if __name__ == "__main__":
    sys.exit(main())