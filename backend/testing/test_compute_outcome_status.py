from app.compute_outcome_status import derive_compute_outcome_status
from app.models import ComputeJobStatus


def _verdict(status: str, met: bool = False) -> dict:
    return {
        "operator": "AND",
        "met": met,
        "criteria": [{"observation_status": status, "met": met}],
    }


def test_execution_and_scientific_truth_are_distinct():
    result = derive_compute_outcome_status(
        ComputeJobStatus.SUCCEEDED, _verdict("MEASURED", met=False)
    )
    assert result == {
        "execution_status": "SUCCEEDED",
        "criterion_status": "NOT_MET",
        "experiment_status": "CRITERION_NOT_MET",
    }


def test_unmeasured_and_cancelled_statuses_are_unambiguous():
    unmeasured = derive_compute_outcome_status(
        ComputeJobStatus.SUCCEEDED, _verdict("UNMEASURED")
    )
    assert unmeasured["experiment_status"] == "CRITERION_UNMEASURED"
    cancelled = derive_compute_outcome_status(
        ComputeJobStatus.CANCELLED, _verdict("MEASURED", met=True)
    )
    assert cancelled["execution_status"] == "CANCELLED"
    assert cancelled["criterion_status"] == "MET"
    assert cancelled["experiment_status"] == "EXECUTION_CANCELLED"
