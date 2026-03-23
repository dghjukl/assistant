from runtime.launch_catalog import BUNDLE_KEYS, LEGACY_SURFACES, bundle_for, normalize_role_name, service_label


def test_role_aliases_and_service_labels_are_canonical():
    assert normalize_role_name("main") == "primary"
    assert normalize_role_name("tools") == "tool"
    assert service_label("primary") == "Main model"
    assert service_label("creativity") == "Creativity helper"


def test_bundle_keys_and_legacy_surface_registry_are_exposed():
    assert BUNDLE_KEYS == ("minimal", "standard", "full", "vision")
    assert bundle_for("standard").roles == ("primary", "tool", "thinking")
    assert LEGACY_SURFACES["launchers/legacy"]["tier"] == "deprecated"
