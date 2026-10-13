"""Version registry reports runtime choices separately from experimental rules."""
import hashlib
import json
from dataclasses import asdict


def registry(settings):
    from .runtime import profile_fingerprint
    from .build_info import source_revision
    profile = asdict(settings.detector)
    return {
        "htf": {"active":settings.detector.rule_version, "status":"active"},
        "ltf": {"active":settings.detector.rule_version, "status":"active"},
        "alt": {"active":settings.alt_config.engine_version, "available":["v1","v2"],
                "structural":"alt-0.4", "structural_status":"display_only", "notifications":"v1/v2"},
        "profile_fingerprint":profile_fingerprint(settings.detector),
        "source_revision":source_revision(),
    }
