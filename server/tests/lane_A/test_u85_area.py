import pytest
from waitless.utility_lookup import LookupArea, area_from_address
ADDR = "1200 W Example St, Apt 4B, Springfield, IL 62701"

def test_u85_2_area_from_full_address():
    assert area_from_address(ADDR) == LookupArea(city="Springfield", state="IL")

@pytest.mark.parametrize("address,city,state", [
    ("500 Main St, Lake View, il", "Lake View", "IL"), ("Springfield, IL", "Springfield", "IL"),
    ("1 Main St, Springfield, IL 62701-1234, USA", "Springfield", "IL"),
    ("9 Elm St, St. Paul, MN 55101", "St. Paul", "MN")])
def test_u85_2_area_variants(address, city, state):
    assert area_from_address(address) == LookupArea(city=city, state=state)

@pytest.mark.parametrize("address", [None, "", "my new apartment", "1200 W Example St, Apt 4B",
    "10 Main St, Paris, ZZ 12345", "1200 W Example St Springfield IL 62701"])
def test_u85_2_area_unparseable(address):
    assert area_from_address(address) is None

def test_u85_2_area_keeps_only_city_and_state():
    assert area_from_address(ADDR).model_dump() == {"city": "Springfield", "state": "IL"}
