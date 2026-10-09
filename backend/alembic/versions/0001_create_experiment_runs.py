"""Create experiment run metadata table."""

import sqlalchemy as sa

from alembic import op

revision = "0001_experiment_runs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experiment_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("data_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("algorithm_version", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')",
            name="ck_experiment_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_experiment_runs_status_created_at",
        "experiment_runs",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_experiment_runs_status_created_at", table_name="experiment_runs")
    op.drop_table("experiment_runs")
