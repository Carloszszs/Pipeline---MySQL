import subprocess
import sys
import time
from pathlib import Path

CONTAINER = "mysql_efimero"
ROOT_PASSWORD = "testroot"
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
        raise RuntimeError(f"'{' '.join(args[:3])}...' falló: {result.stderr.strip()}")
    return result


def mysql_cmd(db=None):
    """docker exec hacia mysql por TCP (el servidor temporal de init no escucha TCP)."""
    cmd = [
        "docker", "exec", "-i", "-e", f"MYSQL_PWD={ROOT_PASSWORD}", CONTAINER,
        "mysql", "-uroot", "-h127.0.0.1", "--protocol=TCP",
    ]
    return cmd + [db] if db else cmd


def wait_until_ready():
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if run(mysql_cmd() + ["-e", "SELECT 1"], check=False).returncode == 0:
            return
        time.sleep(2)
    raise TimeoutError(f"MySQL no estuvo listo en {READY_TIMEOUT_S}s")


def main():
    if not MIGRATION.is_file():
        print(f"No se encontró el script SQL: {MIGRATION}")
        return 1

    # Limpia restos de ejecuciones previas que hayan fallado.
    run(["docker", "rm", "-f", CONTAINER], check=False)

    try:
        print("[1/4] Creando contenedor efímero...")
        run([
            "docker", "run", "-d", "--name", CONTAINER,
            "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PASSWORD}",
            "-e", f"MYSQL_DATABASE={DB_NAME}",
            "mysql:8.0",
        ])

        print("[2/4] Esperando a que MySQL acepte conexiones TCP...")
        wait_until_ready()

        print("[3/4] Aplicando script SQL...")
        with MIGRATION.open("rb") as sql_file:
            run(mysql_cmd(DB_NAME), stdin_file=sql_file)

        print("[4/4] Verificando registros...")
        out = run(mysql_cmd(DB_NAME) + ["-N", "-B", "-e", "SELECT COUNT(*) FROM usuarios;"]).stdout.strip()
        count = int(out)
        if count != EXPECTED_USERS:
            raise AssertionError(f"Se esperaban {EXPECTED_USERS} usuarios y hay {count}")

        print(f"Prueba de restauración completada con éxito ({count} usuarios).")
        return 0
    except Exception as exc:
        print(f"Error durante el proceso: {exc}")
        return 1
    finally:
        print("Limpiando entorno: destruyendo contenedor efímero...")
        run(["docker", "rm", "-f", CONTAINER], check=False)


if __name__ == "__main__":
    sys.exit(main())