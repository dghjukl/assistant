from runtime.launch_catalog import BUNDLE_KEYS, bundle_for, export_catalog, normalize_role_name, service_label


def test_role_aliases_and_service_labels_are_canonical():
    assert normalize_role_name("main") == "primary"
    assert normalize_role_name("tools") == "tool"
    assert service_label("primary") == "Main model"
    assert service_label("creativity") == "Creativity helper"


def test_bundle_keys_and_catalog_surface_are_exposed():
    assert BUNDLE_KEYS == ("minimal", "standard", "full", "vision")
    assert bundle_for("standard").roles == ("primary", "tool", "thinking")
    assert "legacy_surfaces" not in export_catalog()
