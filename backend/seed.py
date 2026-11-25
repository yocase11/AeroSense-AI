from __future__ import annotations

from backend.database import AgentRegistry, ApiSource, City, SessionLocal
from backend.integrations import adapter_catalog
from backend.config import get_settings
from backend.agents import AGENT_REGISTRY


CITIES = (
    ("chennai", "Chennai", 13.0827, 80.2707),
    ("bengaluru", "Bengaluru", 12.9716, 77.5946),
    ("hyderabad", "Hyderabad", 17.3850, 78.4867),
    ("mumbai", "Mumbai", 19.0760, 72.8777),
    ("delhi", "Delhi", 28.6139, 77.2090),
)


def seed_reference_data() -> None:
    settings = get_settings()
    catalog = adapter_catalog(
        openaq_api_key=settings.openaq_api_key,
        firms_map_key=settings.firms_map_key,
        cams_api_key=settings.cams_api_key,
    )
    with SessionLocal.begin() as session:
        for city_id, name, latitude, longitude in CITIES:
            session.merge(
                City(
                    id=city_id,
                    name=name,
                    latitude=latitude,
                    longitude=longitude,
                    country_code="IN",
                )
            )
        for source_id, details in catalog.items():
            source = session.get(ApiSource, source_id)
            if source is None:
                source = ApiSource(
                    id=source_id,
                    display_name=source_id.replace("_", " ").title(),
                    attribution=details["attribution"],
                    configured=details["configured"],
                    active=(
                        source_id != "copernicus_cams" or details["configured"]
                    ),
                )
                session.add(source)
            else:
                source.display_name = source_id.replace("_", " ").title()
                source.attribution = details["attribution"]
                source.configured = details["configured"]
                source.active = (
                    source_id != "copernicus_cams" or details["configured"]
                )
        for agent in AGENT_REGISTRY:
            agent_row = session.get(AgentRegistry, agent["name"])
            if agent_row is None:
                session.add(
                    AgentRegistry(name=agent["name"], role=agent["role"], enabled=True)
                )
            else:
                agent_row.role = agent["role"]
                agent_row.enabled = True


if __name__ == "__main__":
    seed_reference_data()
