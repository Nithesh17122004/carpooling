# Database migration status

The application still uses MongoDB/PyMongo in its active code paths. PostgreSQL dependencies in requirements.txt only prepare the environment; they do not migrate data or switch the application.

Read docs/POSTGRESQL_MIGRATION.md before configuring DATABASE_URL. Do not remove MongoDB or set the app to PostgreSQL until all services, workers, scripts and tests have been migrated and reconciled.