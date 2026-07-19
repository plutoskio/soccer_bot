from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from soccer_bot.config import load_json


INFORMATION_STATES = {
    "pre_lineup_72h_clean_v1",
    "pre_lineup_24h_v1",
    "confirmed_lineup_v1",
}
ELIGIBILITY_FLAGS = {
    "eligible_result_models",
    "eligible_team_models",
    "eligible_player_models",
}
MODEL_ROLES = {"designated", "alternative", "candidate"}
MODEL_STATUSES = {"validated", "experimental", "trained", "planned"}


class FamilyRegistryError(ValueError):
    """Raised when specialized model-family routing is ambiguous or unsafe."""


@dataclass(frozen=True)
class RegisteredModel:
    model_version: str
    role: str
    status: str
    information_states: tuple[str, ...]
    config_path: str
    artifact_path: str | None
    logical_sha256: str | None
    evaluation_record: str | None


@dataclass(frozen=True)
class ModelFamily:
    family_key: str
    display_name: str
    engine_key: str
    eligibility_flag: str
    contract_keys: tuple[str, ...]
    designated_model_version: str | None
    models: tuple[RegisteredModel, ...]

    def model(self, model_version: str) -> RegisteredModel:
        matches = [model for model in self.models if model.model_version == model_version]
        if not matches:
            raise KeyError(model_version)
        return matches[0]

    @property
    def designated_model(self) -> RegisteredModel | None:
        if self.designated_model_version is None:
            return None
        return self.model(self.designated_model_version)


@dataclass(frozen=True)
class SpecializedFamilyRegistry:
    registry_version: str
    sport: str
    information_states: tuple[str, ...]
    families: tuple[ModelFamily, ...]
    market_evidence: dict

    def family(self, family_key: str) -> ModelFamily:
        matches = [family for family in self.families if family.family_key == family_key]
        if not matches:
            raise KeyError(family_key)
        return matches[0]

    def family_for_contract(self, contract_key: str) -> ModelFamily:
        matches = [
            family for family in self.families if contract_key in family.contract_keys
        ]
        if not matches:
            raise KeyError(contract_key)
        return matches[0]


def load_specialized_family_registry(path: Path) -> SpecializedFamilyRegistry:
    return parse_specialized_family_registry(load_json(path))


def parse_specialized_family_registry(
    specification: object,
) -> SpecializedFamilyRegistry:
    if not isinstance(specification, dict):
        raise FamilyRegistryError("Family registry must be an object")
    registry_version = _string(specification, "registry_version")
    sport = _string(specification, "sport")
    if sport != "soccer":
        raise FamilyRegistryError("Family registry sport must be soccer")

    raw_states = specification.get("information_states")
    if (
        not isinstance(raw_states, list)
        or set(raw_states) != INFORMATION_STATES
        or len(raw_states) != len(INFORMATION_STATES)
    ):
        raise FamilyRegistryError(
            "information_states must declare clean T-72, T-24, and confirmed lineup"
        )

    raw_families = specification.get("families")
    if not isinstance(raw_families, list) or not raw_families:
        raise FamilyRegistryError("families must be a non-empty list")

    families = []
    family_keys: set[str] = set()
    contract_owners: dict[str, str] = {}
    global_model_versions: set[str] = set()
    for raw_family in raw_families:
        if not isinstance(raw_family, dict):
            raise FamilyRegistryError("Each family must be an object")
        family_key = _string(raw_family, "family_key")
        if family_key in family_keys:
            raise FamilyRegistryError(f"Duplicate family_key: {family_key}")
        family_keys.add(family_key)
        eligibility_flag = _string(raw_family, "eligibility_flag")
        if eligibility_flag not in ELIGIBILITY_FLAGS:
            raise FamilyRegistryError(
                f"Unsupported eligibility flag for {family_key}: {eligibility_flag}"
            )

        raw_contracts = raw_family.get("contract_keys")
        if not isinstance(raw_contracts, list) or not raw_contracts:
            raise FamilyRegistryError(f"{family_key} contract_keys must be non-empty")
        contract_keys = tuple(_nonempty_string(value, "contract_key") for value in raw_contracts)
        if len(set(contract_keys)) != len(contract_keys):
            raise FamilyRegistryError(f"Duplicate contract in family {family_key}")
        for contract_key in contract_keys:
            if contract_key in contract_owners:
                raise FamilyRegistryError(
                    f"Contract {contract_key} has multiple owners: "
                    f"{contract_owners[contract_key]} and {family_key}"
                )
            contract_owners[contract_key] = family_key

        raw_models = raw_family.get("models")
        if not isinstance(raw_models, list) or not raw_models:
            raise FamilyRegistryError(f"{family_key} models must be non-empty")
        models = []
        local_versions: set[str] = set()
        designated_roles = []
        for raw_model in raw_models:
            model = _parse_model(raw_model, family_key)
            if model.model_version in local_versions:
                raise FamilyRegistryError(
                    f"Duplicate model_version in {family_key}: {model.model_version}"
                )
            if model.model_version in global_model_versions:
                raise FamilyRegistryError(
                    f"Model version belongs to multiple families: {model.model_version}"
                )
            local_versions.add(model.model_version)
            global_model_versions.add(model.model_version)
            if model.role == "designated":
                designated_roles.append(model.model_version)
            models.append(model)

        designated = raw_family.get("designated_model_version")
        if designated is not None:
            designated = _nonempty_string(designated, "designated_model_version")
            if designated not in local_versions:
                raise FamilyRegistryError(
                    f"{family_key} designated model is not registered: {designated}"
                )
            selected = next(model for model in models if model.model_version == designated)
            if selected.role != "designated":
                raise FamilyRegistryError(
                    f"{family_key} designated model must have designated role"
                )
            if selected.status in {"planned", "trained"}:
                raise FamilyRegistryError(
                    f"{family_key} cannot designate an unreleased model"
                )
            if designated_roles != [designated]:
                raise FamilyRegistryError(
                    f"{family_key} must have exactly one matching designated role"
                )
        elif designated_roles:
            raise FamilyRegistryError(
                f"{family_key} has a designated role without designated_model_version"
            )

        families.append(
            ModelFamily(
                family_key=family_key,
                display_name=_string(raw_family, "display_name"),
                engine_key=_string(raw_family, "engine_key"),
                eligibility_flag=eligibility_flag,
                contract_keys=contract_keys,
                designated_model_version=designated,
                models=tuple(models),
            )
        )

    market_evidence = specification.get("market_evidence")
    if not isinstance(market_evidence, dict):
        raise FamilyRegistryError("market_evidence must be an object")
    if market_evidence.get("independent_model_feature") is not False:
        raise FamilyRegistryError("Market evidence cannot be an independent-model feature")
    if market_evidence.get("require_timestamp_safe_mapping") is not True:
        raise FamilyRegistryError("Market evidence must require timestamp-safe mapping")

    return SpecializedFamilyRegistry(
        registry_version=registry_version,
        sport=sport,
        information_states=tuple(raw_states),
        families=tuple(families),
        market_evidence=market_evidence,
    )


def _parse_model(value: object, family_key: str) -> RegisteredModel:
    if not isinstance(value, dict):
        raise FamilyRegistryError(f"Each model in {family_key} must be an object")
    model_version = _string(value, "model_version")
    role = _string(value, "role")
    status = _string(value, "status")
    if role not in MODEL_ROLES:
        raise FamilyRegistryError(f"Unsupported role for {model_version}: {role}")
    if status not in MODEL_STATUSES:
        raise FamilyRegistryError(f"Unsupported status for {model_version}: {status}")
    raw_states = value.get("information_states")
    if not isinstance(raw_states, list) or not raw_states:
        raise FamilyRegistryError(f"{model_version} information_states must be non-empty")
    states = tuple(_nonempty_string(item, "information_state") for item in raw_states)
    if len(set(states)) != len(states) or not set(states).issubset(INFORMATION_STATES):
        raise FamilyRegistryError(f"{model_version} has invalid information states")

    config_path = _safe_relative_path(value.get("config_path"), "config_path")
    artifact_path = _optional_safe_path(value.get("artifact_path"), "artifact_path")
    logical_sha256 = value.get("logical_sha256")
    evaluation_record = _optional_safe_path(
        value.get("evaluation_record"), "evaluation_record"
    )
    if status in {"validated", "experimental", "trained"}:
        if artifact_path is None:
            raise FamilyRegistryError(f"{model_version} requires artifact_path")
        if not _is_sha256(logical_sha256):
            raise FamilyRegistryError(f"{model_version} requires logical_sha256")
        if evaluation_record is None:
            raise FamilyRegistryError(f"{model_version} requires evaluation_record")
    elif logical_sha256 is not None or artifact_path is not None:
        raise FamilyRegistryError(
            f"Planned model {model_version} cannot claim an artifact or hash"
        )
    if status == "validated" and role != "designated":
        raise FamilyRegistryError(f"Validated model {model_version} must be designated")

    return RegisteredModel(
        model_version=model_version,
        role=role,
        status=status,
        information_states=states,
        config_path=config_path,
        artifact_path=artifact_path,
        logical_sha256=logical_sha256,
        evaluation_record=evaluation_record,
    )


def _string(value: dict, key: str) -> str:
    return _nonempty_string(value.get(key), key)


def _nonempty_string(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FamilyRegistryError(f"{key} must be a non-empty string")
    return value


def _optional_safe_path(value: object, key: str) -> str | None:
    if value is None:
        return None
    return _safe_relative_path(value, key)


def _safe_relative_path(value: object, key: str) -> str:
    path = PurePosixPath(_nonempty_string(value, key))
    if path.is_absolute() or ".." in path.parts:
        raise FamilyRegistryError(f"{key} must stay inside the repository")
    return path.as_posix()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
