def test_fixture_library_scans_with_no_diagnostics(library):
    # A broken fixture would otherwise only show up as a confusing failure in some other test.
    assert library.diagnostics == []
