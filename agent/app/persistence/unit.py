"""Bundle of repositories over one database, injected into services and API handlers."""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database
from .repositories import (
    ApplicationsRepository,
    ApprovalsRepository,
    AuditRepository,
    JobsRepository,
    MetricsRepository,
    PairingRepository,
    ProfileRepository,
    SettingsRepository,
    TasksRepository,
)


@dataclass(slots=True)
class Repositories:
    db: Database
    settings: SettingsRepository
    pairings: PairingRepository
    audit: AuditRepository
    metrics: MetricsRepository
    profile: ProfileRepository
    jobs: JobsRepository
    applications: ApplicationsRepository
    tasks: TasksRepository
    approvals: ApprovalsRepository

    @classmethod
    def build(cls, db: Database) -> "Repositories":
        return cls(
            db=db,
            settings=SettingsRepository(db),
            pairings=PairingRepository(db),
            audit=AuditRepository(db),
            metrics=MetricsRepository(db),
            profile=ProfileRepository(db),
            jobs=JobsRepository(db),
            applications=ApplicationsRepository(db),
            tasks=TasksRepository(db),
            approvals=ApprovalsRepository(db),
        )
