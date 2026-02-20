"""
Migration script to add title column to task_instances table
Run this once to update your database schema
"""

from db import get_db
from sqlalchemy import text

def migrate():
    db = get_db()
    try:
        # Add title column to task_instances
        print("Adding title column to task_instances...")
        db.execute(text("""
            ALTER TABLE task_instances
            ADD COLUMN title VARCHAR(140) DEFAULT '' NOT NULL
        """))

        # Populate title from template for existing instances
        print("Populating title from templates for existing instances...")
        db.execute(text("""
            UPDATE task_instances
            SET title = (
                SELECT task_templates.title
                FROM task_templates
                WHERE task_templates.id = task_instances.template_id
            )
            WHERE title = '' OR title IS NULL
        """))

        db.commit()
        print("Migration completed successfully!")

    except Exception as e:
        db.rollback()
        print(f"Migration failed: {e}")
        print("If column already exists, migration may have already run.")
    finally:
        db.close()

if __name__ == "__main__":
    migrate()
