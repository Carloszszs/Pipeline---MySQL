-- v1_esquema_inicial.sql
-- Idempotente: se puede ejecutar N veces sin errores ni duplicados.

CREATE TABLE IF NOT EXISTS usuarios (
    id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
    nombre     VARCHAR(100) NOT NULL,
    email      VARCHAR(150) NOT NULL,
    creado_en  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_usuarios_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS logs_auditoria (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    usuario_id  INT UNSIGNED    NULL,              -- mismo tipo que usuarios.id
    accion      VARCHAR(255)    NOT NULL,
    fecha       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Cubre la FK (usuario_id es la columna líder) y consultas "acciones de un usuario por fecha".
    KEY idx_logs_usuario_fecha (usuario_id, fecha),
    CONSTRAINT fk_logs_usuario
        FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
        ON DELETE SET NULL      -- la auditoría se conserva aunque se borre el usuario
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Datos de prueba (idealmente mover a seeds/ y no correr en producción).
-- Requiere MySQL >= 8.0.19 (alias de fila, reemplaza al VALUES() obsoleto).
INSERT INTO usuarios (nombre, email) VALUES
    ('Usuario Prueba 1', 'prueba1@empresa.com'),
    ('Usuario Prueba 2', 'prueba2@empresa.com') AS nuevo
ON DUPLICATE KEY UPDATE nombre = nuevo.nombre;