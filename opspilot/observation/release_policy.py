"""Trusted, package-local release calibration catalog; never a runtime override."""

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Field,
    ModelWrapValidatorHandler,
    PrivateAttr,
    TypeAdapter,
    ValidationInfo,
    field_serializer,
    model_validator,
)

from opspilot.domain.base import DTO, Positive, Text


def storable_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("INVALID_TEXT")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("INVALID_TEXT") from None
    return value


def strict_format_version(value: Any) -> int:
    if type(value) is not int or value != 1:
        raise ValueError("INVALID_FORMAT_VERSION")
    return value


FormatVersion = Annotated[Literal[1], BeforeValidator(strict_format_version)]
_APPROVAL_TIMESTAMP = TypeAdapter(AwareDatetime)
ReleaseText = Annotated[Text, AfterValidator(storable_text)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RELEASE_POLICY_CATALOG_PATH = (
    Path(__file__).parent / "policies/release-calibrations.json"
)


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class ReleasePolicyContent(DTO):
    # Canonical JSON sorts keys/removes whitespace; it does not rewrite the
    # lexical content of a valid timestamp string covered by the policy hash.
    _approved_at_text: str | None = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def preserve_approval_text(
        cls, value: Any, handler: ModelWrapValidatorHandler[Self], info: ValidationInfo
    ) -> Self:
        original = value.get("approved_at") if isinstance(value, dict) else None
        if info.mode == "json" and isinstance(original, str):
            try:
                storable_text(original)
                # Preserve exactly the strict AwareDatetime JSON grammar before
                # handing its Python datetime to the strict wrap handler.
                value = {
                    **value,
                    "approved_at": _APPROVAL_TIMESTAMP.validate_json(
                        json.dumps(original), strict=True
                    ),
                }
            except ValueError:
                raise ValueError("INVALID_APPROVAL_TIME") from None
        result = handler(value)
        if isinstance(original, str):
            result._approved_at_text = original
        return result

    @field_serializer("approved_at", when_used="json")
    def serialize_approval(self, value: datetime) -> str:
        return self._approved_at_text or value.isoformat().replace("+00:00", "Z")

    format_version: FormatVersion
    target: dict[ReleaseText, ReleaseText]
    health_profile_revision: ReleaseText
    health_profile_sha256: Sha256
    min_tracking_seconds: Positive
    evaluator: Literal["release-after-v1"]
    mapping: Literal["pod-uid-replicaset-image-v1"]
    evidence_path: ReleaseText
    evidence_sha256: Sha256
    approval_ref: ReleaseText
    approved_at: AwareDatetime

    @model_validator(mode="after")
    def valid_target_and_path(self) -> "ReleasePolicyContent":
        if set(self.target) != {
            "integration_id",
            "cluster_uid",
            "namespace",
            "resource_uid",
            "workload",
        }:
            raise ValueError("INVALID_POLICY_TARGET")
        path = PurePosixPath(self.evidence_path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.evidence_path:
            raise ValueError("INVALID_EVIDENCE_PATH")
        return self


class ReleasePolicy(DTO):
    policy_id: ReleaseText
    revision: ReleaseText
    content_sha256: Sha256
    content: ReleasePolicyContent

    @model_validator(mode="after")
    def valid_hash(self) -> "ReleasePolicy":
        digest = sha256(canonical_json(self.content.model_dump(mode="json")).encode())
        if (
            digest != self.content_sha256
            or self.revision != f"{self.policy_id}@{digest[:12]}"
        ):
            raise ValueError("INVALID_POLICY_HASH")
        return self


class ReleasePolicyCatalog(DTO):
    format_version: FormatVersion
    policies: tuple[ReleasePolicy, ...]

    @model_validator(mode="after")
    def unique_revisions(self) -> "ReleasePolicyCatalog":
        if len({p.revision for p in self.policies}) != len(self.policies):
            raise ValueError("DUPLICATE_POLICY_REVISION")
        return self


def load_release_policy_catalog() -> ReleasePolicyCatalog:
    return ReleasePolicyCatalog.model_validate_json(
        RELEASE_POLICY_CATALOG_PATH.read_bytes()
    )
