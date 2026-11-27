from app.reference_catalogue import reference_names


def test_reference_catalogue_contains_all_unique_names():
    names = reference_names()
    assert len(names) == len(set(names)) == 2357
    assert 'apple' in names
    assert 'zzounds' in names
