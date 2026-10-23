from .conftest import PREFIX
async def test_dump(hass, loaded_entry):
    for s in hass.states.async_all():
        if PREFIX in s.entity_id and s.domain=="sensor":
            print(s.entity_id, repr(s.state[:300]))
    print({k: (v if len(str(v))<200 else str(v)[:200]) for k,v in loaded_entry.runtime_data.attributes.get("current_charging_rule",{}).items()})
