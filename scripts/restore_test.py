"""Prueba efímera de restauración en MySQL 8.0 (Windows / Linux / macOS).

Uso (desde cualquier directorio):
    python scripts/restore_test.py
"""
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

IMAGE = "mysql:8.0"
CONTAINER = f"mysql_efimero_{os.getpid()}"      # único: permite ejecuciones en paralelo
ROOT_PASSWORD = secrets.token_urlsafe(16)       # sin credenciales fijas en el repo
DB_NAME = "db_restore_test"
EXPECTED_USERS = 2
READY_TIMEOUT_S = 90
MIGRATION = Path(__file__).resolve().parent.parent / "migraciones" / "v1_esquema_inicial.sql"


def run(args, stdin_file=None, check=True, timeout=120):
    """Ejecuta un comando SIN shell (lista de argumentos): igual en Windows y Linux."""
    result = subprocess.run(
        args,
        stdin=stdin_file,
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
    """docker exec hacia mysql por TCP (el servidor temporal de init no escucha TCP)."""
    cmd = [
        "docker", "exec", "-i", "-e", f"MYSQL_PWD={ROOT_PASSWORD}", CONTAINER,
        "mysql", "-uroot", "-h127.0.0.1", "--protocol=TCP",
    ]
    return cmd + [db] if db else cmd


def apply_migration():
    with MIGRATION.open("rb") as sql_file:      # se reabre en cada llamada: offset limpio
        run(mysql_cmd(DB_NAME), stdin_file=sql_file)


def count_users():
    out = run(mysql_cmd(DB_NAME) + ["-N", "-B", "-e", "SELECT COUNT(*) FROM usuarios;"]).stdout.strip()
    return int(out)


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
    except Exception as exc:                    # el diagnóstico nunca debe romper el flujo
        print(f"No se pudieron obtener los logs: {exc}")


def cleanup():
    """Nunca lanza excepciones: no debe enmascarar el error original."""
    print("Limpiando entorno: destruyendo contenedor efímero...")
    try:
        # -v elimina también volúmenes anónimos asociados al contenedor
        r = run(["docker", "rm", "-f", "-v", CONTAINER], check=False, timeout=60)
        if r.returncode != 0 and "No such container" not in r.stderr:
            print(f"ADVERTENCIA: no se pudo eliminar {CONTAINER}: {r.stderr.strip()}")
    except Exception as exc:
        print(f"ADVERTENCIA: fallo en la limpieza de {CONTAINER}: {exc}")


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt                      # fuerza la ejecución del finally


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    created = False
    try:
        if shutil.which("docker") is None:
            print("Docker no está instalado o no está en el PATH.")
            return 1
        if not MIGRATION.is_file():
            print(f"No se encontró el script SQL: {MIGRATION}")
            return 1
        if run(["docker", "info"], check=False, timeout=30).returncode != 0:
            print("El daemon de Docker no está disponible.")
            return 1

        print("[1/5] Creando contenedor efímero...")
        created = True                           # marcado ANTES: un 'run' fallido puede dejar residuos
        run([
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PASSWORD}",
            "-e", f"MYSQL_DATABASE={DB_NAME}",
            "--tmpfs", "/var/lib/mysql",         # datos en RAM: rápido y sin volúmenes huérfanos en disco
            IMAGE,
            "--performance-schema=OFF",          # ahorra cientos de MB de RAM
            "--innodb-buffer-pool-size=64M",
        ])

        print("[2/5] Esperando a que MySQL acepte conexiones TCP...")
        wait_until_ready()

        print("[3/5] Aplicando script SQL...")
        apply_migration()
        count = count_users()
        if count != EXPECTED_USERS:
            raise AssertionError(f"Se esperaban {EXPECTED_USERS} usuarios y hay {count}")

        print("[4/5] Verificando idempotencia (segunda ejecución)...")
        apply_migration()
        count = count_users()
        if count != EXPECTED_USERS:
            raise AssertionError(f"Tras re-ejecutar hay {count} usuarios (no es idempotente)")

        print(f"[5/5] Prueba de restauración completada con éxito ({count} usuarios).")
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