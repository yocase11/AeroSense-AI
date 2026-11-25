"""Add user accounts, revocable sessions, and login throttling."""
from alembic import op
import sqlalchemy as sa


revision = "0002_authentication"
down_revision = "0001_platform_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("role IN ('viewer', 'analyst', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "auth_login_throttles",
        sa.Column("bucket_key", sa.String(length=64), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auth_login_throttles_window",
        "auth_login_throttles",
        ["window_started_at"],
    )
    op.add_column(
        "investigations", sa.Column("owner_user_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_investigations_owner_user_id_users",
        "investigations",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_investigations_owner_user_id",
        "investigations",
        ["owner_user_id"],
    )
    op.add_column("reports", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_reports_owner_user_id_users",
        "reports",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_reports_owner_user_id", "reports", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_reports_owner_user_id", table_name="reports")
    op.drop_constraint("fk_reports_owner_user_id_users", "reports", type_="foreignkey")
    op.drop_column("reports", "owner_user_id")
    op.drop_index("ix_investigations_owner_user_id", table_name="investigations")
    op.drop_constraint(
        "fk_investigations_owner_user_id_users",
        "investigations",
        type_="foreignkey",
    )
    op.drop_column("investigations", "owner_user_id")
    op.drop_index(
        "ix_auth_login_throttles_window", table_name="auth_login_throttles"
    )
    op.drop_table("auth_login_throttles")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
