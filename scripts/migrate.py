"""Motor de migraciones incrementales para MySQL 8.0, vía `docker exec`.

Uso (desde cualquier directorio):
    python scripts/migrate.py                 # aplica las pendientes en el contenedor 'mysql_local'
    python scripts/migrate.py --status        # muestra aplicadas / pendientes (no modifica nada)
    python scripts/migrate.py --up-to 1       # aplica solo hasta v1
    python scripts/migrate.py --container otro --database otra_db

La contraseña y la base por defecto se leen de las variables del propio contenedor
(MYSQL_ROOT_PASSWORD / MYSQL_DATABASE): no se duplican credenciales en el repo.

Convención de archivos en migraciones/:  v<N>_<descripcion>.sql
  - El orden es NUMÉRICO (v2 va antes que v10), no alfabético.
  - Cada archivo debe terminar en ';'.
  - Una migración ya aplicada NO se edita (se detecta por checksum): se crea una nueva versión.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migraciones"
DEFAULT_CONTAINER = "mysql_local"
FILENAME_RE = re.compile(r"^v(\d+)_([A-Za-z0-9_]+)\.sql$")   # charset restringido: seguro de interpolar

# Infraestructura del motor (no es una migración): se crea siempre, de forma idempotente.
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
    """Ejecuta un comando SIN shell (lista de argumentos): igual en Windows y Linux."""
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


class Target:
    """Un servidor MySQL dentro de un contenedor Docker (local persistente o efímero)."""

    def __init__(self, container, database=None):
        self.container = container
        out = run(["docker", "inspect", "-f", "{{json .Config.Env}}", container], timeout=15).stdout
        env = dict(item.split("=", 1) for item in json.loads(out))
        self.password = env.get("MYSQL_ROOT_PASSWORD")
        self.database = database or env.get("MYSQL_DATABASE")
        if not self.password:
            raise RuntimeError(f"El contenedor '{container}' no define MYSQL_ROOT_PASSWORD.")
        if not self.database:
            raise RuntimeError("No se pudo determinar la base de datos; usa --database.")

    def _cmd(self, with_db=True):
        # Por TCP: el servidor temporal del entrypoint solo escucha por socket.
        cmd = ["docker", "exec", "-i", "-e", f"MYSQL_PWD={self.password}", self.container,
               "mysql", "-uroot", "-h127.0.0.1", "--protocol=TCP"]
        return cmd + [self.database] if with_db else cmd

    def ping(self):
        return run(self._cmd(False) + ["-e", "SELECT 1"], check=False, timeout=10).returncode == 0

    def execute(self, sql):
        return run(self._cmd(), input_text=sql)

    def query(self, sql):
        """Filas como listas de columnas (salida batch separada por tabuladores)."""
        out = run(self._cmd() + ["-N", "-B", "-e", sql]).stdout
        return [line.split("\t") for line in out.splitlines() if line]

    def scalar(self, sql):
        return int(self.query(sql)[0][0])


def load_migrations(directory=MIGRATIONS_DIR):
    """Lee y valida los archivos .sql; devuelve la lista ordenada por versión numérica."""
    found = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(f"Nombre inválido: {path.name} (formato: v<N>_<descripcion>.sql)")
        version = int(match.group(1))
        if version in found:
            raise ValueError(f"Versión duplicada v{version}: {found[version].filename} y {path.name}")

        raw = path.read_bytes().replace(b"\r\n", b"\n")   # mismo checksum con CRLF (Windows) o LF (CI)
        sql = raw.decode("utf-8-sig")
        if not sql.rstrip().endswith(";"):
            raise ValueError(f"{path.name} debe terminar en ';'")
        found[version] = Migration(version, path.name, hashlib.sha256(raw).hexdigest(), sql)
    return [found[v] for v in sorted(found)]


def fetch_applied(target):
    """{version: (filename, checksum, applied_at)}; vacío si schema_migrations aún no existe."""
    exists = target.scalar(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'schema_migrations'"
    )
    if not exists:
        return {}
    rows = target.query("SELECT version, filename, checksum, applied_at FROM schema_migrations ORDER BY version")
    return {int(v): (fname, chk, at) for v, fname, chk, at in rows}


def migrate(target, up_to=None, directory=MIGRATIONS_DIR):
    """Aplica las migraciones pendientes en orden. Devuelve la lista de versiones aplicadas."""
    target.execute(BOOTSTRAP_SQL)

    migrations = load_migrations(directory)
    known = {m.version: m for m in migrations}
    applied = fetch_applied(target)

    # 1) Lo ya aplicado debe seguir existiendo y no haber cambiado.
    for version, (filename, checksum, _) in applied.items():
        if version not in known:
            raise RuntimeError(f"v{version} ({filename}) está en la BD pero falta en {directory.name}/")
        if known[version].checksum != checksum:
            raise RuntimeError(f"checksum distinto en v{version}: {filename} se modificó después de aplicarse")

    # 2) Aplicar pendientes en orden, sin permitir "huecos hacia atrás".
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
        # SQL + registro en UNA sola invocación: el cliente se detiene en el primer error,
        # así el INSERT solo corre si todo el archivo se ejecutó bien.
        record = (f"\nINSERT INTO schema_migrations (version, filename, checksum) "
                  f"VALUES ({m.version}, '{m.filename}', '{m.checksum}');\n")
        target.execute(m.sql + record)
        newly_applied.append(m.version)
        last_applied = m.version
    return newly_applied


def print_status(target, directory=MIGRATIONS_DIR):
    migrations = load_migrations(directory)
    known = {m.version for m in migrations}
    applied = fetch_applied(target)
    for m in migrations:
        if m.version in applied:
            _, checksum, at = applied[m.version]
            estado = "aplicada" if checksum == m.checksum else "MODIFICADA"
            print(f"  [{estado:<10}] v{m.version:<3} {m.filename}  ({at})")
        else:
            print(f"  [{'pendiente':<10}] v{m.version:<3} {m.filename}")
    for version, (filename, _, _) in applied.items():
        if version not in known:
            print(f"  [{'SIN ARCHIVO':<10}] v{version:<3} {filename}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aplica migraciones SQL incrementales a MySQL en Docker.")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help=f"nombre del contenedor (def.: {DEFAULT_CONTAINER})")
    parser.add_argument("--database", help="base de datos (def.: MYSQL_DATABASE del contenedor)")
    parser.add_argument("--up-to", type=int, metavar="N", help="aplicar solo hasta la versión N")
    parser.add_argument("--status", action="store_true", help="mostrar estado sin modificar nada")
    args = parser.parse_args(argv)

    try:
        target = Target(args.container, args.database)
        print(f"Destino: contenedor '{target.container}', base '{target.database}'")
        if args.status:
            print_status(target)
            return 0
        applied = migrate(target, args.up_to)
        print(f"Migraciones aplicadas: {applied}" if applied else "Nada pendiente: la base ya está al día.")
        return 0
    except FileNotFoundError:
        print("Error: Docker no está instalado o no está en el PATH.")
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}")
        if "No such" in str(exc):
            print("¿Está levantado el contenedor? Prueba: docker compose up -d")
    return 1


if __name__ == "__main__":
    sys.exit(main())