"""Motor de migraciones incrementales + prueba efímera en MySQL 8.0."""
import hashlib
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

IMAGE = "mysql:8.0"
CONTAINER = f"mysql_efimero_{os.getpid()}"
ROOT_PASSWORD = secrets.token_urlsafe(16)
DB_NAME = "db_restore_test"
READY_TIMEOUT_S = 90
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migraciones"
FILENAME_RE = re.compile(r"^v(\d+)_([A-Za-z0-9_]+)\.sql$")

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INT UNSIGNED NOT NULL,
    filename   VARCHAR(255) NOT NULL,
    checksum   CHAR(64)     NOT NULL,
    applied_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
"""


class Migration(NamedTuple):
    version: int
    filename: str
    checksum: str
    sql: str


def run(args, input_text=None, check=True, timeout=120):
    result = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:2])} falló (código {result.returncode}): {result.stderr.strip()}")
    return result


def mysql_cmd(db=None):
    cmd = [
        "docker", "exec", "-i", "-e", f"MYSQL_PWD={ROOT_PASSWORD}", CONTAINER,
        "mysql", "-uroot", "-h127.0.0.1", "--protocol=TCP",
    ]
    return cmd + [db] if db else cmd


def query(sql):
    out = run(mysql_cmd(DB_NAME) + ["-N", "-B", "-e", sql]).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def scalar(sql):
    return int(query(sql)[0][0])


def load_migrations(directory=MIGRATIONS_DIR):
    found = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(f"Nombre inválido: {path.name} (formato: v<N>_<descripcion>.sql)")
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"Versión duplicada v{version}: {found[version].filename} y {path.name}")

        raw = path.read_bytes().replace(b"\r\n", b"\n")
        sql = raw.decode("utf-8-sig")
        if not sql.rstrip().endswith(";"):
            raise ValueError(f"{path.name} debe terminar en ';'")
        found[version] = Migration(version, path.name, hashlib.sha256(raw).hexdigest(), sql)
    return [found[v] for v in sorted(found)]


def migrate(up_to=None):
    run(mysql_cmd(DB_NAME), input_text=BOOTSTRAP_SQL)

    migrations = load_migrations()
    known = {m.version: m for m in migrations}
    applied = {int(v): (fname, chk) for v, fname, chk in
               query("SELECT version, filename, checksum FROM schema_migrations")}

    for version, (filename, checksum) in applied.items():
        if version not in known:
            raise RuntimeError(f"v{version} ({filename}) está en la BD pero falta en {MIGRATIONS_DIR.name}/")
        if known[version].checksum != checksum:
            raise RuntimeError(f"checksum distinto en v{version}: {filename} se modificó después de aplicarse")

    last_applied = max(applied, default=0)
    newly_applied = []
    for m in migrations:
        if m.version in applied:
            continue
        if up_to is not None and m.version > up_to:
            break
        if m.version < last_applied:
            raise RuntimeError(f"{m.filename} es anterior a la última aplicada (v{last_applied}); renumérala")

        print(f"    -> aplicando {m.filename}")
        record = (f"\nINSERT INTO schema_migrations (version, filename, checksum) "
                  f"VALUES ({m.version}, '{m.filename}', '{m.checksum}');\n")
        run(mysql_cmd(DB_NAME), input_text=m.sql + record)
        newly_applied.append(m.version)
        last_applied = m.version
    return newly_applied


def wait_until_ready():
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        state = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], check=False, timeout=10)
        if state.stdout.strip() != "true":
            raise RuntimeError("El contenedor se detuvo durante la inicialización.")
        try:
            if run(mysql_cmd() + ["-e", "SELECT 1"], check=False, timeout=10).returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    raise TimeoutError(f"MySQL no estuvo listo en {READY_TIMEOUT_S}s")


def dump_logs():
    try:
        logs = run(["docker", "logs", "--tail", "20", CONTAINER], check=False, timeout=15)
        print("--- Últimas líneas del log del contenedor ---")
        print((logs.stdout + logs.stderr).strip())
    except Exception as exc:
        print(f"No se pudieron obtener los logs: {exc}")


def cleanup():
    print("Limpiando entorno: destruyendo contenedor efímero...")
    try:
        r = run(["docker", "rm", "-f", "-v", CONTAINER], check=False, timeout=60)
        if r.returncode != 0 and "No such container" not in r.stderr:
            print(f"ADVERTENCIA: no se pudo eliminar {CONTAINER}: {r.stderr.strip()}")
    except Exception as exc:
        print(f"ADVERTENCIA: fallo en la limpieza de {CONTAINER}: {exc}")


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def check(label, actual, expected):
    if actual != expected:
        raise AssertionError(f"{label}: esperado {expected!r}, obtenido {actual!r}")


def has_telefono():
    return scalar(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA='{DB_NAME}' AND TABLE_NAME='usuarios' AND COLUMN_NAME='telefono'"
    )


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    created = False
    try:
        if shutil.which("docker") is None:
            print("Docker no está instalado o no está en el PATH.")
            return 1
        if run(["docker", "info"], check=False, timeout=30).returncode != 0:
            print("El daemon de Docker no está disponible.")
            return 1

        all_versions = [m.version for m in load_migrations()]
        later_versions = [v for v in all_versions if v > 1]

        print("[1/6] Creando contenedor efímero...")
        created = True
        run([
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PASSWORD}",
            "-e", f"MYSQL_DATABASE={DB_NAME}",
            "--tmpfs", "/var/lib/mysql",
            IMAGE,
            "--performance-schema=OFF",
            "--innodb-buffer-pool-size=64M",
        ])

        print("[2/6] Esperando a que MySQL acepte conexiones TCP...")
        wait_until_ready()

        print("[3/6] Instalación inicial: solo hasta v1 (simula una BD existente con datos)...")
        check("migraciones aplicadas", migrate(up_to=1), [1])
        check("usuarios tras v1", scalar("SELECT COUNT(*) FROM usuarios"), 2)
        check("telefono antes de v2", has_telefono(), 0)

        print("[4/6] Upgrade: aplicando migraciones pendientes sobre datos existentes...")
        check("migraciones aplicadas", migrate(), later_versions)
        check("usuarios tras upgrade", scalar("SELECT COUNT(*) FROM usuarios"), 2)
        check("telefono tras v2", has_telefono(), 1)
        check("telefono NULL en filas previas", scalar("SELECT COUNT(*) FROM usuarios WHERE telefono IS NULL"), 2)
        check("filas en schema_migrations", scalar("SELECT COUNT(*) FROM schema_migrations"), len(all_versions))

        print("[5/6] Idempotencia: re-ejecutar el motor no debe aplicar nada...")
        check("migraciones aplicadas", migrate(), [])

        print("[6/6] Integridad: una migración modificada debe ser detectada...")
        run(mysql_cmd(DB_NAME) + ["-e", "UPDATE schema_migrations SET checksum=REPEAT('0',64) WHERE version=1"])
        try:
            migrate()
        except RuntimeError as exc:
            if "checksum" not in str(exc):
                raise
        else:
            raise AssertionError("El motor no detectó una migración modificada")

        print("Prueba completada con éxito.")
        return 0
    except KeyboardInterrupt:
        print("Interrumpido por el usuario o por el sistema.")
        return 130
    except Exception as exc:
        print(f"Error durante el proceso: {exc}")
        if created:
            dump_logs()
        return 1
    finally:
        if created:
            cleanup()


if __name__ == "__main__":
    sys.exit(main())