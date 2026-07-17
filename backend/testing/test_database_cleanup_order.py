import pytest

from app.models import Project
from app.strategic_resident_checkpointing import initialize_strategic_resident
from testing.conftest import _postgres_cleanup_tables


def test_postgres_cleanup_deletes_resident_before_checkpoint_children():
    """The cleanup order must break the Resident/checkpoint FK cycle safely."""
    names = [table.name for table in _postgres_cleanup_tables()]

    assert names.index("strategicresident") < names.index(
        "strategicresidentcheckpoint"
    )
    assert names.index("strategicresident") < names.index(
        "strategicoperationreceipt"
    )


@pytest.mark.asyncio
async def test_db_session_teardown_handles_resident_checkpoint_cycle(db_session):
    """Regression: PostgreSQL teardown must not clear a live checkpoint pointer."""
    project = Project(
        name="Resident cleanup-order regression",
        user_id="cleanup-order-test",
        github_owner="test",
        github_repo="resident-cleanup",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    resident = await initialize_strategic_resident(db_session, project.id)
    await db_session.commit()

    assert resident.current_checkpoint_id is not None
