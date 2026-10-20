from sqlalchemy import inspect, text

from app.extensions import db


def _add_missing_columns(table_name, definitions):
    inspector = inspect(db.engine)
    existing = {column['name'] for column in inspector.get_columns(table_name)}
    added = set()

    with db.engine.begin() as connection:
        for column_name, definition in definitions.items():
            if column_name not in existing:
                connection.execute(
                    text(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}')
                )
                added.add(column_name)

    return added


def apply_schema_migrations():
    """Apply idempotent additive changes needed by existing installations."""
    company_columns = _add_missing_columns('companies', {
        'is_parent_company': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'company_type': "VARCHAR(50) NOT NULL DEFAULT 'external'",
        'has_full_access': 'BOOLEAN NOT NULL DEFAULT FALSE',
    })
    binary_type = 'BYTEA' if db.engine.dialect.name == 'postgresql' else 'BLOB'
    _add_missing_columns('time_entries', {
        'project_id_external': 'VARCHAR(100)',
        'project_name': 'VARCHAR(200)',
        'resources': 'JSON',
        'description': 'TEXT',
        'total_amount': 'NUMERIC(15, 2) NOT NULL DEFAULT 0',
        'assigned_payment_team': 'VARCHAR(50)',
        'invoice_generated': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'invoice_number': 'VARCHAR(50)',
        'invoice_pdf': binary_type,
        'invoice_pdf_filename': 'VARCHAR(255)',
        'invoice_generated_by': 'INTEGER',
        'invoice_generated_at': 'TIMESTAMP',
    })
    _add_missing_columns('service_items', {
        'invoice_pdf': binary_type,
        'invoice_pdf_filename': 'VARCHAR(255)',
    })

    if 'company_type' in company_columns:
        with db.engine.begin() as connection:
            connection.execute(text("""
                UPDATE companies
                SET is_parent_company = TRUE,
                    company_type = 'parent',
                    has_full_access = TRUE
                WHERE name = 'LOLC Technologies'
            """))

    with db.engine.begin() as connection:
        connection.execute(text("""
            UPDATE companies
            SET company_type = 'external'
            WHERE company_type = 'finance'
              AND is_parent_company = FALSE
        """))
        connection.execute(text("""
            UPDATE companies
            SET is_parent_company = TRUE,
                has_full_access = TRUE
            WHERE company_type = 'parent'
        """))
