"""Add job type, progress and result metadata to experiment runs."""

import sqlalchemy as sa

from alembic import op

revision = "0002_experiment_run_progress"
down_revision = "0001_experiment_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "experiment_runs",
        sa.Column(
            "job_type", sa.String(length=24), server_default=sa.text("'mtsp'"), nullable=False
        ),
    )
    op.add_column(
        "experiment_runs",
        sa.Column("progress_percent", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("experiment_runs", sa.Column("progress_stage", sa.String(length=240)))
    op.add_column("experiment_runs", sa.Column("log_tail", sa.Text()))
    op.add_column("experiment_runs", sa.Column("worker_pid", sa.Integer()))
    op.add_column("experiment_runs", sa.Column("result_path", sa.Text()))
    op.add_column("experiment_runs", sa.Column("result_summary", sa.JSON()))
    op.add_column(
        "experiment_runs",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.create_check_constraint(
            "ck_experiment_runs_progress_percent",
            "progress_percent >= 0 AND progress_percent <= 100",
        )


def downgrade() -> None:
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.drop_constraint("ck_experiment_runs_progress_percent", type_="check")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("result_summary")
        batch_op.drop_column("result_path")
        batch_op.drop_column("worker_pid")
        batch_op.drop_column("log_tail")
        batch_op.drop_column("progress_stage")
        batch_op.drop_column("progress_percent")
        batch_op.drop_column("job_type")
