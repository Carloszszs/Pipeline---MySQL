-- v2_agregar_columna_telefono.sql
-- Cambio estructural sobre tabla con datos existentes.
ALTER TABLE usuarios
    ADD COLUMN telefono VARCHAR(20) NULL;