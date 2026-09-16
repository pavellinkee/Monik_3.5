"""Режим сканирования у возможности.

Проход Level 1 стал именованным режимом со своим порогом
(``the_main_rules.md``, правило 10). Level 2 обязан подтверждать
возможность **той же** планкой, которой она была найдена, иначе частый
проход с мягким порогом находил бы то, что проверка со строгим порогом
отвергает, и работа обоих уровней тратилась бы впустую.

Значение по умолчанию ``ur`` относится к уже существующим записям: они
созданы до появления режимов, когда проход был один и соответствовал
основному.
"""

from __future__ import annotations

from monik.infrastructure.db.migrations.base import Migration

__all__ = ["MIGRATION"]

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE opportunities ADD COLUMN mode TEXT NOT NULL DEFAULT 'ur'",
)

MIGRATION = Migration(version=3, name="scan_mode", statements=_STATEMENTS)
