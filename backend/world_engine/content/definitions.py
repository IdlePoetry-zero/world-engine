"""Versioned, human-authored seed definitions for the shipped backgrounds."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegionSeed:
    name: str
    description: str
    terrain: str
    main_output: str
    hazard: str


@dataclass(frozen=True, slots=True)
class PolitySeed:
    name: str
    government: str
    public_law: str
    resource_focus: str


@dataclass(frozen=True, slots=True)
class OrganizationSeed:
    name: str
    organization_type: str
    mission: str
    operating_principle: str


@dataclass(frozen=True, slots=True)
class PersonSeed:
    role: str
    goal_title: str
    goal: str
    values: tuple[str, ...]
    traits: tuple[str, ...]
    secret: str


@dataclass(frozen=True, slots=True)
class DiscoverySiteSeed:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class InvestigationSeed:
    artifact_name: str
    artifact_description: str
    witness_claim: str
    archival_observation: str


@dataclass(frozen=True, slots=True)
class RegionalOccupationProfile:
    """Authored role weights and specialist skills for one regional roster."""

    role_weights: dict[str, int]
    role_skill_levels: dict[str, dict[str, str]]


OPENING_INVESTIGATIONS = {
    "low_magic_feudal": InvestigationSeed(
        "渡口旧值守账板", "一块带轮班刻痕和旧日期的木板，边缘有渡口印记。",
        "听人说，争议通行费生效的那一刻，这个聚落的渡口已经停止值守。",
        "争议通行费生效当刻的值守栏有一笔已完成的过渡登记。",
    ),
    "modern_city": InvestigationSeed(
        "车站维修柜值班单", "一张印有站务日期与班次编号的维修柜底单。",
        "听调度员说，改线通知生效那一刻，本地站务已经停办。",
        "改线通知生效当刻，值班单仍有一笔在岗受理记录。",
    ),
    "long_life_cultivation": InvestigationSeed(
        "旧泉分水刻石拓片", "纸上有磨损的时辰标记和旧分水契的印记。",
        "传闻旧水契换文当刻，这个聚落已经暂停分水。",
        "拓片中的旧时辰栏显示，换文当刻仍登记了一轮分水。",
    ),
}


@dataclass(frozen=True, slots=True)
class BackgroundDefinition:
    pack_id: str
    version: str
    title: str
    subtitle: str
    summary: str
    setting: str
    year_days: int
    calendar_name: str
    currency_id: str
    currency_name: str
    resources: tuple[str, ...]
    regions: tuple[RegionSeed, ...]
    polities: tuple[PolitySeed, ...]
    organizations: tuple[OrganizationSeed, ...]
    key_names: tuple[tuple[str, str], ...]
    key_people: tuple[PersonSeed, ...]
    extra_given_names: tuple[str, ...]
    extra_family_names: tuple[str, ...]
    extra_roles: tuple[str, ...]
    settlement_names: tuple[str, ...]
    settlement_kinds: tuple[str, ...]
    themes: tuple[str, ...]
    systems: tuple[str, ...]
    scenario_hooks: tuple[str, ...]
    discovery_site: DiscoverySiteSeed
    rules: dict[str, object]
    regional_occupation_profiles: dict[str, RegionalOccupationProfile] | None = None


LOW_MAGIC = BackgroundDefinition(
    pack_id="low_magic_feudal",
    version="1.7.0",
    title="The Broken Crown",
    subtitle="Low magic feudal lands",
    summary="A network of river fiefs, chartered towns and old obligations. Small, costly magic exists, while harvest, travel, law and allegiance remain material constraints.",
    setting="The crown is weak after a succession dispute. Grain, bridges, kinship, service and reputation shape daily life. A few trained practitioners can read omens or bind an oath, but these gifts carry costs and do not replace practical action.",
    year_days=360,
    calendar_name="calendar.fief_year_360",
    currency_id="feudal.silver_mark",
    currency_name="silver marks",
    resources=("feudal.grain", "feudal.timber", "feudal.iron", "feudal.salt", "feudal.medicine", "feudal.wool", "feudal.fresh_water"),
    regions=(
        RegionSeed("Ashen March", "A defended border of pine roads and old watch keeps.", "pine_highland", "timber", "winter banditry"),
        RegionSeed("Nine Bridges Vale", "A fertile river basin whose bridges collect tolls and news.", "river_vale", "grain", "spring floods"),
        RegionSeed("Redcliff Coast", "A steep coast of salt yards, fishing coves and rival harbors.", "rocky_coast", "salt", "storm seasons"),
        RegionSeed("Hearthmoor", "A broad grazing plain divided by family claims and cattle tracks.", "open_moor", "wool", "late-summer drought"),
        RegionSeed("Old Crownwood", "A dense forest around the abandoned royal hunting road.", "ancient_forest", "timber", "uncertain boundaries"),
        RegionSeed("Copper Hills", "A chain of small mines and foundries under competing charters.", "mineral_hills", "iron", "mine collapses"),
        RegionSeed("Pilgrim Road", "A market corridor connecting shrines, inns and abbey farms.", "roadland", "medicine", "pilgrim congestion"),
        RegionSeed("Misted Fens", "A wetland of raised paths, reed harvests and hidden ferries.", "wetland", "grain", "fog and fever"),
        RegionSeed("South Barleylands", "A granary of tenant farms and seasonal threshing towns.", "farmland", "grain", "rent disputes"),
        RegionSeed("White Pass", "A narrow mountain passage whose keep controls winter movement.", "alpine_pass", "iron", "snow closure"),
        RegionSeed("Lakeward Cantons", "A ring of lakeside villages tied together by boat guilds.", "lake_district", "salt", "shoreline disputes"),
        RegionSeed("The Orchard Belt", "An old fruit-growing region with dense kin networks.", "orchard_country", "medicine", "blight and debt"),
    ),
    polities=(
        PolitySeed("Duchy of Greyhaven", "hereditary_duchy", "A duke may levy tolls only on chartered crossings.", "iron"),
        PolitySeed("Free Barony of Rookfield", "elective_barony", "Local reeves hear tenant and boundary disputes.", "grain"),
        PolitySeed("Crown March of Alderwatch", "royal_march", "The warden must maintain the public road and beacon chain.", "timber"),
        PolitySeed("Abbey Lands of Saint Orra", "abbey_domain", "The abbess controls mills but owes grain relief in famine.", "grain"),
        PolitySeed("Harbor League of Redcliff", "chartered_council", "Harbor dues require a recorded public schedule.", "salt"),
        PolitySeed("Hearthmoor Moot", "clan_moot", "Pasture access follows the seasonal moot compact.", "wool"),
        PolitySeed("Coppice Principality", "hereditary_principality", "Forest cutting is limited by the winter commons law.", "timber"),
        PolitySeed("Copper Hill Compact", "mine_charter", "Mine owners owe repair and burial funds before dividend.", "iron"),
        PolitySeed("Pilgrim Road Wardens", "road_custodianship", "Waystations must shelter travelers during declared storms.", "medicine"),
        PolitySeed("Misted Fen Reeveholds", "reeve_council", "Ferry rights are shared among households on the marked path.", "grain"),
        PolitySeed("South Barley Crownlands", "royal_estate", "The crown may not seize seed grain held for next sowing.", "grain"),
        PolitySeed("White Pass March", "military_march", "The pass warden may close the road only for a stated hazard.", "iron"),
        PolitySeed("Lakeward Cantons", "canton_federation", "Lake routes remain open to all registered boat guilds.", "salt"),
        PolitySeed("Orchard Duchy of Vey", "hereditary_duchy", "Fruit tithes are due after household winter stores are reserved.", "medicine"),
        PolitySeed("Lowbridge Free City", "chartered_city", "A public court hears guild and resident complaints.", "grain"),
        PolitySeed("Barony of Flintmere", "hereditary_barony", "Iron dues cannot be collected twice on one caravan.", "iron"),
        PolitySeed("Three Abbeys Concord", "religious_concord", "Common granaries open by a recorded famine vote.", "grain"),
        PolitySeed("Northwood Kinlands", "kinship_council", "No family may bar the shared winter trail.", "timber"),
        PolitySeed("Saltmere Coast", "harbor_council", "Salt pan leases expire at the end of the flood season.", "salt"),
        PolitySeed("Red Field Ward", "reeve_assembly", "Emergency labor is compensated in food or coin by custom.", "grain"),
        PolitySeed("March of Seven Torches", "royal_march", "Beacon maintenance is a levy shared by each village.", "timber"),
        PolitySeed("Stonegate Principality", "hereditary_principality", "The gate toll is waived for medical and burial carts.", "medicine"),
        PolitySeed("Eastmere Boat Cantons", "canton_federation", "A boat seizure requires two witnesses and a written cause.", "salt"),
        PolitySeed("Vintner's Reach", "chartered_estate", "Harvest contracts bind only the named crop and season.", "wool"),
    ),
    organizations=(
        OrganizationSeed("Greyhaven Carters' Guild", "trade_guild", "Keep the river road serviceable and rates legible.", "published toll books"),
        OrganizationSeed("Rookfield Grain Factors", "merchant_house", "Move grain between village stores and winter markets.", "sealed measures"),
        OrganizationSeed("Alderwatch Beacon Order", "watch_order", "Maintain warning fires without claiming private land.", "rotating duty"),
        OrganizationSeed("Saint Orra Infirmary", "charitable_house", "Provide basic care and record scarce medicines.", "need before rank"),
        OrganizationSeed("Redcliff Saltwrights", "craft_guild", "Keep salt production safe and coordinate harbor sales.", "shared works"),
        OrganizationSeed("Hearthmoor Drovers' Union", "labor_guild", "Protect cattle routes and settle wage disputes.", "witnessed contracts"),
        OrganizationSeed("Crownwood Foresters", "stewardship_order", "Track legal timber cuts and protect common paths.", "seasonal limits"),
        OrganizationSeed("Copper Hill Founders", "craft_guild", "Supply tools and repair mine equipment.", "repair reserve first"),
        OrganizationSeed("Pilgrim Road Hostels", "hospitality_network", "Keep food and beds available along the route.", "posted prices"),
        OrganizationSeed("Fen Ferry Cooperative", "transport_cooperative", "Maintain raised paths and share ferry shifts.", "community schedule"),
        OrganizationSeed("South Barley Millers", "millers_guild", "Process harvests and publish milling fees.", "weighed grain"),
        OrganizationSeed("White Pass Rescue Crew", "rescue_company", "Guide and recover travelers during pass closures.", "hazard before fee"),
        OrganizationSeed("Lakeward Boatwrights", "craft_guild", "Build repairable craft for the lake routes.", "common measurements"),
        OrganizationSeed("Vey Orchard Keepers", "growers_cooperative", "Share blight reports and protect seed stock.", "seed reserve"),
        OrganizationSeed("Lowbridge Scribes' Hall", "professional_guild", "Maintain contracts, appeals and public records.", "copy for both parties"),
        OrganizationSeed("Flintmere Miners' Lodge", "worker_lodge", "Coordinate safety, burial funds and dispute hearings.", "safety stop rule"),
        OrganizationSeed("Three Bells Bellfounders", "craft_guild", "Repair civic bells and keep a shared apprentice list.", "open apprenticeship"),
        OrganizationSeed("Northwood Healers' Circle", "care_network", "Exchange tested remedies and maintain herb stores.", "evidence before claim"),
        OrganizationSeed("Saltmere Netmakers", "craft_guild", "Supply fishing crews and mend public harbor nets.", "repair over replacement"),
        OrganizationSeed("Red Field Harvest Hands", "labor_union", "Negotiate day wages and emergency harvest terms.", "named day rates"),
        OrganizationSeed("Seven Torches Couriers", "courier_company", "Carry sealed letters between beacon towns.", "chain of custody"),
        OrganizationSeed("Stonegate Apothecaries", "medical_guild", "Dispense scarce remedies with recorded batches.", "batch provenance"),
        OrganizationSeed("Eastmere Boat Factors", "merchant_house", "Coordinate boat cargo and prevent duplicate claims.", "cargo receipts"),
        OrganizationSeed("Vintner's Reach Cooperage", "craft_guild", "Supply barrels and restore damaged storage.", "reusable stock"),
    ),
    key_names=(
        ("Mara", "Voss"), ("Edric", "Vale"), ("Tamsin", "Rowe"), ("Oren", "Wick"),
        ("Ilya", "Mere"), ("Celia", "Thorne"), ("Bram", "Sayer"), ("Nessa", "Dale"),
        ("Corin", "Ashby"), ("Lysa", "Fen"), ("Hollis", "Grey"), ("Perrin", "West"),
        ("Anwen", "Cairn"), ("Jory", "Bell"), ("Sable", "Hart"), ("Mikel", "Reed"),
        ("Elowen", "March"), ("Tobin", "Fell"), ("Runa", "Pike"), ("Garran", "Moss"),
        ("Mira", "Crow"), ("Oswin", "Lark"), ("Yara", "Noll"), ("Dain", "Brook"),
        ("Etta", "Rill"), ("Rafe", "Hale"), ("Noor", "Bracken"), ("Lio", "Firth"),
        ("Ione", "Frost"), ("Tarek", "Holt"), ("Sera", "Quill"), ("Venn", "Marsh"),
    ),
    key_people=(
        PersonSeed("village reeve", "Keep the mill open", "Secure a fair milling schedule before the next levy.", ("fairness", "duty"), ("patient", "practical"), "Has privately promised a cousin a share of the mill's repair contract."),
        PersonSeed("disputed heir", "Prove the boundary claim", "Find witnesses who remember the old stone boundary.", ("family", "truth"), ("proud", "observant"), "Knows one witness was paid to alter a harvest record."),
        PersonSeed("road captain", "Protect the winter road", "Keep the southern pass open without exhausting the watch.", ("safety", "service"), ("decisive", "guarded"), "Has been diverting a small part of the watch budget to a rescue fund."),
        PersonSeed("itinerant physician", "Rebuild the medicine store", "Trace which herb shipments spoiled and restore reliable supply.", ("care", "evidence"), ("curious", "steady"), "Suspects the last fever came from a contaminated well, not bad air."),
        PersonSeed("salt harbor factor", "End the tariff dispute", "Negotiate a public harbor schedule that smaller boats can afford.", ("order", "trade"), ("calculating", "courteous"), "Keeps a second ledger of unofficial storm-shelter favors."),
        PersonSeed("drover organizer", "Secure a shared pasture", "Renew the seasonal pasture compact before the herd moves.", ("solidarity", "custom"), ("direct", "loyal"), "Has been hiding a younger sibling from an unlawful levy."),
        PersonSeed("forester", "Stop the illegal cutting", "Identify who is cutting the commons beyond the seasonal limit.", ("stewardship", "truth"), ("quiet", "persistent"), "Once allowed a desperate family to take timber without filing a record."),
        PersonSeed("mine engineer", "Brace the lower shaft", "Win support for a costly repair before the next shift descends.", ("safety", "craft"), ("methodical", "stubborn"), "Knows the mine owner has an unregistered second exit."),
        PersonSeed("hostel keeper", "Keep rooms available", "Balance pilgrim shelter with the inn's winter debt.", ("hospitality", "independence"), ("warm", "wary"), "Has a sealed letter addressed to a traveler who never arrived."),
        PersonSeed("fen ferrier", "Repair the raised path", "Coordinate households before floodwater cuts off the ferry.", ("community", "competence"), ("resourceful", "blunt"), "Has been carrying messages for two feuding families without telling either."),
        PersonSeed("mill apprentice", "Earn a full charter", "Show the millers that careful measures can settle the flour dispute.", ("skill", "respect"), ("inventive", "anxious"), "Can identify which official weight was altered, but fears naming the clerk."),
        PersonSeed("pass rescuer", "Retire a dangerous rope bridge", "Convince merchants to fund a safer crossing before snowfall.", ("safety", "honor"), ("brave", "impatient"), "Was the only survivor of a previous collapse and carries guilt for leaving a companion."),
        PersonSeed("boatwright", "Train a new crew", "Keep repairs affordable while teaching two young apprentices.", ("craft", "legacy"), ("careful", "generous"), "Has hidden a damaged boat from a guild inspector until the owner can repay the loan."),
        PersonSeed("orchard steward", "Contain the blight", "Compare orchards and isolate the infected grafts before spring.", ("care", "learning"), ("analytical", "reserved"), "A respected grower introduced the graft now suspected of spreading disease."),
        PersonSeed("charter scribe", "Correct the public roll", "Resolve a mismatch between the tax roll and the town's signed charter.", ("accuracy", "justice"), ("precise", "cautious"), "Knows the town clerk omitted a widow's name to protect a patron."),
        PersonSeed("miners' speaker", "Win a safety stop", "Make the owners accept a rule that closes unsafe shafts.", ("solidarity", "safety"), ("forceful", "empathetic"), "Has received a warning note from someone inside the owners' council."),
        PersonSeed("bell founder", "Restore the dawn bell", "Find a replacement bronze supply without taking the abbey's grain reserve.", ("craft", "service"), ("inventive", "proud"), "The old bell has a hidden maker's mark that undermines a noble's claim."),
        PersonSeed("herbalist", "Test a fever remedy", "Compare patient outcomes before recommending the new bark tea.", ("evidence", "care"), ("patient", "skeptical"), "A relative is selling an untested version under the same name."),
        PersonSeed("netmaker", "Protect the shared nets", "Set a repair roster after a storm tore the harbor nets.", ("cooperation", "fairness"), ("social", "firm"), "Has been covering a fisher's debt by quietly mending their nets after hours."),
        PersonSeed("harvest forewoman", "Finish the harvest safely", "Hire enough hands without accepting the lord's unsafe night schedule.", ("dignity", "safety"), ("decisive", "protective"), "Can expose the steward's false wage count but it would cost several families their work."),
        PersonSeed("courier", "Deliver an overdue letter", "Find a route that avoids the closed beacon road.", ("reliability", "curiosity"), ("quick", "restless"), "The letter is not sealed by its listed sender."),
        PersonSeed("apothecary", "Replace a lost batch", "Trace the missing medicine and keep its remaining supply for urgent cases.", ("care", "prudence"), ("precise", "protective"), "Has reserved a dose for a former enemy who once saved their life."),
        PersonSeed("cargo factor", "Reconcile the boat receipts", "Find where two traders claim the same crate of salt.", ("honesty", "trade"), ("diplomatic", "skeptical"), "The missing receipt is in the possession of a trusted boat captain."),
        PersonSeed("cooper", "Recover the barrel stock", "Reuse sound casks and source only the timber the shop truly needs.", ("frugality", "craft"), ("practical", "generous"), "Has lent half the good barrels to a family brewing a funeral ale."),
        PersonSeed("abbey gardener", "Save the seed beds", "Protect spring seed from a late frost without burning scarce fuel.", ("stewardship", "patience"), ("gentle", "resourceful"), "Knows the abbey plans to sell land that contains the best seed plots."),
        PersonSeed("minor noble", "Keep a family compact", "Prevent a marriage alliance from turning into a private feud.", ("family", "peace"), ("charming", "conflicted"), "Has no intention of honoring the marriage promise made in their name."),
        PersonSeed("traveling singer", "Recover a stolen instrument", "Learn who took the instrument and whether it was sold or pawned.", ("art", "independence"), ("perceptive", "playful"), "Recognized a patron's voice in a supposedly anonymous threat."),
        PersonSeed("village midwife", "Reopen the birthing room", "Secure clean linens and a trained helper before the next due date.", ("care", "competence"), ("steady", "plainspoken"), "Has been sheltering a traveler whose identity would upset the reeve."),
        PersonSeed("watch lantern keeper", "Replace the broken lenses", "Get the beacon visible before the storm line reaches the coast.", ("duty", "craft"), ("meticulous", "quiet"), "One lens was deliberately broken to signal a smuggler's boat."),
        PersonSeed("landless veteran", "Find a lawful home", "Secure a plot or paid work without giving up a promised pension.", ("dignity", "loyalty"), ("guarded", "reliable"), "Was ordered to leave a village after refusing an unlawful eviction."),
        PersonSeed("town mediator", "Settle a water-share pact", "Keep millers and households from fighting over the summer channel.", ("balance", "peace"), ("measured", "observant"), "Has a private water map showing the channel was redirected years ago."),
        PersonSeed("bridge reeve", "Keep the crossing open", "Agree on a repair levy that does not exclude poorer households.", ("access", "fairness"), ("practical", "principled"), "A wealthy trader offered to pay for repairs in exchange for a private toll exemption."),
    ),
    extra_given_names=("Alda", "Beren", "Cora", "Davin", "Eris", "Fenn", "Galen", "Hessa", "Ivor", "Jessa", "Kellan", "Lina", "Marek", "Nori", "Orla", "Pavel", "Quin", "Rhea", "Sorin", "Tilda", "Ulric", "Vela", "Wren", "Yestin"),
    extra_family_names=("Alder", "Briar", "Cobb", "Dun", "Elm", "Fallow", "Grove", "Harrow", "Ives", "Jarrow", "Kerr", "Loom", "Marden", "North", "Oaks", "Pell", "Quince", "Rook", "Stave", "Tanner", "Upton", "Vane", "Wold", "Yarrow"),
    extra_roles=("tenant farmer", "baker", "carter", "weaver", "ferryman", "miller", "apprentice smith", "herbal gatherer", "scribe", "watch keeper", "fisher", "midwife", "courier", "cook", "wheelwright", "shepherd", "barrel maker", "porter", "tanner", "inn worker", "boat hand", "forester", "market seller", "school keeper"),
    settlement_names=("Crossing", "Market", "Mill", "Green", "Landing", "Gate", "Coppice", "Spring", "Hall"),
    settlement_kinds=("market_town", "farm_village", "workshop_quarter", "ferry", "watch_post", "forest_hamlet", "wellstead", "charter_ward", "road_inn"),
    themes=("obligation", "inheritance", "scarce harvest", "public records", "kinship", "seasonal travel"),
    systems=("feudal_governance", "agrarian_production", "chartered_markets", "limited_oathcraft", "seasonal_transport", "multidimensional_relations"),
    scenario_hooks=("A disputed bridge levy threatens the grain route.", "A fever medicine batch has two competing origin stories.", "A succession promise conflicts with the tenant commons compact."),
    discovery_site=DiscoverySiteSeed(
        name="Ashford Ferry Mark",
        description=(
            "A slate hidden beneath a reed bridge preserves ferry tallies from before the levy dispute. "
            "Its scraped dates suggest the toll marker moved before the abbey reported the flood damage."
        ),
    ),
    rules={
        "magic": {"enabled": True, "scope": "limited", "domains": ["oath_binding", "herbal_lore", "omens"], "costs": ["time", "scarce materials", "social obligation"]},
        "government": {"succession": "custom_and_charter", "land_rights": "recorded_claims", "taxes": "named_levies"},
        "economy": {"production": "seasonal", "currency": "feudal.silver_mark", "resource_origin_required": True},
        "combat": {"lethal_force": "possible", "injuries_persist": True, "withdrawal_allowed": True},
    },
)


MODERN_CITY = BackgroundDefinition(
    pack_id="modern_city",
    version="1.7.0",
    title="Twelve Districts",
    subtitle="Contemporary urban society",
    summary="A connected modern world of neighborhoods, transit, employers, civic agencies and households. Reputation, law, work schedules and infrastructure create consequences without supernatural abilities.",
    setting="A metropolitan federation is facing a costly transit rebuild, rising rents and a strained public health network. People rely on phones, transport, public records and local relationships. No supernatural powers exist in this ruleset.",
    year_days=365,
    calendar_name="calendar.civic_year_365",
    currency_id="modern.credit",
    currency_name="credits",
    resources=("modern.food", "modern.fuel", "modern.medicine", "modern.materials", "modern.transit_capacity", "modern.data_bandwidth", "modern.water"),
    regions=(
        RegionSeed("Harbor Ward", "A working waterfront with docks, markets and flood barriers.", "coastal_city", "materials", "storm surge"),
        RegionSeed("North Station", "A rail-centered district where commuters, vendors and night crews overlap.", "transit_node", "transit_capacity", "service disruption"),
        RegionSeed("Old Town", "A dense historic neighborhood with small businesses and contested renovations.", "historic_grid", "food", "rent displacement"),
        RegionSeed("University Quarter", "A student and research district with clinics and shared housing.", "campus_district", "data_bandwidth", "housing pressure"),
        RegionSeed("East Works", "A manufacturing and repair belt adapting to new safety standards.", "industrial_belt", "materials", "workplace injury"),
        RegionSeed("Civic Center", "A government and court district with public services and crowded offices.", "civic_core", "data_bandwidth", "backlog"),
        RegionSeed("South Gardens", "A residential garden district with markets, schools and care workers.", "residential_green", "food", "heat waves"),
        RegionSeed("West Exchange", "A financial and logistics district with uneven wealth and long shifts.", "commercial_core", "fuel", "debt pressure"),
        RegionSeed("Riverline", "A riverfront district connected by buses, ferries and cycle routes.", "river_city", "food", "flood closure"),
        RegionSeed("Hillview", "A hillside neighborhood with stairs, clinics and limited vehicle access.", "urban_hills", "medicine", "accessibility gaps"),
        RegionSeed("Airport Loop", "A logistics ring of hotels, warehouses and all-night services.", "transport_ring", "fuel", "labor shortages"),
        RegionSeed("South Market", "A diverse market district whose vendors coordinate deliveries and public space.", "market_grid", "food", "supply price swings"),
    ),
    polities=(
        PolitySeed("Harbor Federation", "representative_council", "Public procurement and safety decisions are appealable.", "materials"),
        PolitySeed("North Transit Authority", "regional_authority", "Service cuts require a published accessibility review.", "transit_capacity"),
        PolitySeed("Old Town Municipality", "municipal_council", "Tenant relocation requires notice and a documented appeal.", "food"),
        PolitySeed("University Quarter District", "municipal_council", "Campus housing rules apply equally to private dormitories.", "data_bandwidth"),
        PolitySeed("East Works Borough", "municipal_council", "Worksite safety stops are protected from retaliation.", "materials"),
        PolitySeed("Civic Center City", "mayoral_council", "Public records are retained with redaction for private data.", "data_bandwidth"),
        PolitySeed("South Gardens Borough", "municipal_council", "Schools and clinics receive heat emergency priority.", "food"),
        PolitySeed("West Exchange State", "regional_legislature", "Debt collection must identify a review route.", "fuel"),
        PolitySeed("Riverline District", "watershed_council", "Flood barriers are managed across jurisdiction boundaries.", "food"),
        PolitySeed("Hillview Borough", "municipal_council", "Public service routes must be accessible to mobility aids.", "medicine"),
        PolitySeed("Airport Loop Authority", "transport_authority", "Night-shift transit minimums are fixed by contract.", "fuel"),
        PolitySeed("South Market Municipality", "municipal_council", "Vendor permits are reviewed on a public calendar.", "food"),
        PolitySeed("North Coast State", "regional_legislature", "Emergency construction requires a neighborhood hearing.", "materials"),
        PolitySeed("Midline Republic", "parliamentary_republic", "Transit budgets are reported by route and quarter.", "transit_capacity"),
        PolitySeed("Lake District Government", "regional_council", "Water and energy contracts disclose service interruptions.", "medicine"),
        PolitySeed("Central Counties", "county_federation", "Clinic access plans include rural and urban routes.", "medicine"),
        PolitySeed("Riverside Republic", "parliamentary_republic", "River use permits include downstream impact review.", "food"),
        PolitySeed("Greenbelt Region", "regional_assembly", "Protected green corridors remain open to public travel.", "food"),
        PolitySeed("Port Union", "port_authority", "Dock automation changes require worker transition plans.", "materials"),
        PolitySeed("Industrial Compact", "regional_council", "Air-quality notices name affected streets and duration.", "materials"),
        PolitySeed("Upper Valley State", "regional_legislature", "Water restrictions follow published household tiers.", "food"),
        PolitySeed("South Coast Republic", "parliamentary_republic", "Public evacuation routes must be maintained each year.", "fuel"),
        PolitySeed("Highland District", "regional_council", "Emergency services coordinate across transit boundaries.", "medicine"),
        PolitySeed("Market Belt Federation", "federal_council", "Small vendors have equal access to public procurement.", "food"),
    ),
    organizations=(
        OrganizationSeed("Harbor Mutual Aid Network", "mutual_aid", "Coordinate flood supplies and household check-ins.", "consent-based support"),
        OrganizationSeed("Night Rail Workers Union", "labor_union", "Protect night-shift staffing, safety and service continuity.", "bargained schedules"),
        OrganizationSeed("Old Town Tenants' Assembly", "tenant_union", "Share notices and coordinate lawful rent appeals.", "documented cases"),
        OrganizationSeed("Campus Clinic Cooperative", "health_cooperative", "Share appointment capacity and community health referrals.", "privacy-preserving care"),
        OrganizationSeed("East Works Repair Collective", "worker_cooperative", "Repair equipment and train apprentices for safer jobs.", "safety before throughput"),
        OrganizationSeed("Civic Records Watch", "civic_group", "Explain public records and flag unexplained service gaps.", "verify primary records"),
        OrganizationSeed("South Gardens Food Hub", "food_cooperative", "Coordinate neighborhood food deliveries and refrigeration.", "fair access"),
        OrganizationSeed("West Exchange Credit Union", "financial_cooperative", "Offer small loans and debt counseling to members.", "transparent fees"),
        OrganizationSeed("Riverline Ferry Service", "transport_cooperative", "Keep river crossings reliable during road disruptions.", "accessible timetables"),
        OrganizationSeed("Hillview Caregivers Network", "care_network", "Match respite care and accessible transport.", "caregiver consent"),
        OrganizationSeed("Airport Loop Logistics Union", "labor_union", "Negotiate safe night work and predictable rosters.", "posted shifts"),
        OrganizationSeed("South Market Vendor Council", "merchant_association", "Coordinate deliveries, permits and shared storage.", "open membership"),
        OrganizationSeed("Public Water Lab", "research_cooperative", "Test local water and publish understandable findings.", "reproducible results"),
        OrganizationSeed("Midline Transit Riders", "civic_group", "Track route access and present rider evidence at hearings.", "rider-led agenda"),
        OrganizationSeed("Lake District Energy Co-op", "utility_cooperative", "Maintain local backup power for clinics and homes.", "shared reserves"),
        OrganizationSeed("Central Counties Clinic Exchange", "health_network", "Coordinate urgent referrals between clinics.", "minimum necessary data"),
        OrganizationSeed("Riverside Legal Aid Desk", "legal_aid", "Help residents understand contracts, notices and review rights.", "confidential advice"),
        OrganizationSeed("Greenbelt Trail Stewards", "stewardship_group", "Maintain public routes and report access barriers.", "open trails"),
        OrganizationSeed("Port Union Freight Cooperative", "logistics_cooperative", "Share cargo capacity and reduce empty vehicle trips.", "verified manifests"),
        OrganizationSeed("Industrial Air Monitor Team", "research_cooperative", "Measure street-level air quality and publish time series.", "public methods"),
        OrganizationSeed("Upper Valley Repair Bank", "repair_cooperative", "Lend tools and extend equipment life across towns.", "repair before discard"),
        OrganizationSeed("South Coast Evacuation Volunteers", "volunteer_network", "Help households plan for storms and mobility needs.", "no forced entry"),
        OrganizationSeed("Highland Access Coalition", "accessibility_group", "Document gaps in transit and emergency access.", "user-tested routes"),
        OrganizationSeed("Market Belt Small Suppliers", "merchant_cooperative", "Help small suppliers bid for public contracts.", "plain-language bids"),
    ),
    key_names=(
        ("Amira", "Santos"), ("Jonah", "Bell"), ("Kei", "Morris"), ("Nadia", "Chen"),
        ("Luis", "Okafor"), ("Mina", "Patel"), ("Rafael", "Nguyen"), ("Priya", "Torres"),
        ("Ellis", "Carter"), ("Tariq", "Ali"), ("Mae", "Kim"), ("Sofia", "Reed"),
        ("Darius", "Green"), ("Noor", "Haddad"), ("Rowan", "Price"), ("Lena", "Brooks"),
        ("Mateo", "Rivera"), ("June", "Park"), ("Asha", "Cole"), ("Omar", "Lewis"),
        ("Vera", "Bennett"), ("Sam", "Diaz"), ("Inez", "Foster"), ("Milo", "Washington"),
        ("Rina", "Ward"), ("Theo", "Khan"), ("Celeste", "Morgan"), ("Anil", "James"),
        ("Harper", "Singh"), ("Zara", "Wright"), ("Micah", "Lopez"), ("Eden", "Bailey"),
    ),
    key_people=(
        PersonSeed("night rail dispatcher", "Restore the late service", "Reconcile crew limits with the route needed by hospital staff.", ("safety", "reliability"), ("focused", "fair"), "Knows a shift swap was entered under a supervisor's account."),
        PersonSeed("tenant organizer", "Keep the block housed", "Help neighbors respond to a redevelopment notice before the hearing.", ("dignity", "community"), ("persistent", "empathetic"), "Has a private purchase offer from a developer and has not disclosed it."),
        PersonSeed("water lab analyst", "Trace the contamination", "Compare samples from three neighborhoods before naming a source.", ("evidence", "public health"), ("methodical", "cautious"), "A preliminary result implicates a major employer that funds the lab."),
        PersonSeed("clinic coordinator", "Reduce missed appointments", "Build a referral path for residents without reliable transit.", ("care", "privacy"), ("organized", "warm"), "Has been covering a relative's appointments without recording the conflict."),
        PersonSeed("repair collective steward", "Prevent a lockout", "Negotiate access to the repair bay after an ownership dispute.", ("solidarity", "craft"), ("practical", "diplomatic"), "A machine's safety certificate was altered after the last inspection."),
        PersonSeed("public records reporter", "Verify the contract", "Confirm which route costs changed before publishing a story.", ("truth", "accountability"), ("curious", "skeptical"), "Has an anonymous source who is also a rival candidate."),
        PersonSeed("food hub manager", "Keep the cold room running", "Find a short-term power source before donated food spoils.", ("care", "stewardship"), ("resourceful", "calm"), "One supplier is quietly substituting lower-quality goods."),
        PersonSeed("credit counselor", "Avoid a predatory loan", "Help a family renegotiate debt while keeping the credit union solvent.", ("fairness", "prudence"), ("patient", "direct"), "A board member privately asked for an exception to a fee rule."),
        PersonSeed("ferry captain", "Keep the crossing accessible", "Coordinate a safe boarding adjustment during construction.", ("access", "service"), ("steady", "inventive"), "Has been using an unapproved landing that shortens the route."),
        PersonSeed("caregiver advocate", "Secure respite hours", "Get an agency to honor the agreed care plan for three households.", ("care", "consent"), ("assertive", "compassionate"), "A client has asked them to keep a safety concern private."),
        PersonSeed("warehouse steward", "Repair the night roster", "Find a safer schedule without losing the contract.", ("safety", "solidarity"), ("blunt", "loyal"), "Has the staffing data that proves an accident risk but was asked not to share it."),
        PersonSeed("market vendor", "Keep a permit", "Gather enough evidence to contest a closure notice.", ("independence", "fairness"), ("social", "resourceful"), "Has shared storage space that is not listed on the permit."),
        PersonSeed("transit advocate", "Protect the clinic route", "Show the authority how a route cut affects appointment access.", ("access", "evidence"), ("persistent", "measured"), "A family member works for the contractor proposing the cut."),
        PersonSeed("tenant attorney", "Prevent an unlawful eviction", "Build a clear record of notice and repair requests.", ("justice", "privacy"), ("precise", "empathetic"), "Has been asked to represent both a tenant and their guarantor."),
        PersonSeed("energy co-op technician", "Keep backup power ready", "Replace a failing inverter before the next heat warning.", ("reliability", "community"), ("hands-on", "careful"), "The replacement part is owned by a former partner who has refused payment."),
        PersonSeed("school nurse", "Restore medication access", "Resolve a clinic handoff delay without exposing a student's private record.", ("care", "confidentiality"), ("calm", "firm"), "Knows the shortage is caused by an internal inventory error."),
        PersonSeed("legal aid volunteer", "Clarify a contract", "Help a worker understand a disputed termination clause.", ("access", "accuracy"), ("patient", "plainspoken"), "The employer asked them to review an unrelated case for free."),
        PersonSeed("trail steward", "Reopen a public path", "Document a barrier and agree on a safe temporary route.", ("access", "stewardship"), ("observant", "cooperative"), "A landowner has offered personal payment to delay the report."),
        PersonSeed("freight scheduler", "Resolve a missing crate", "Check the manifest trail before a supplier misses its deadline.", ("reliability", "honesty"), ("analytical", "restless"), "An overnight scan was manually cleared by a close friend."),
        PersonSeed("air monitor technician", "Publish the readings", "Validate a sensor calibration before the next council meeting.", ("truth", "public health"), ("meticulous", "quiet"), "A sensor was moved after a complaint from a business owner."),
        PersonSeed("tool library coordinator", "Prevent tool loss", "Create a borrowing system that works for shift workers.", ("access", "trust"), ("inventive", "friendly"), "A regular borrower has been taking tools home for a family repair job."),
        PersonSeed("storm volunteer", "Plan a safe evacuation", "Help a mobility-limited household reach a shelter before the alert.", ("safety", "consent"), ("decisive", "gentle"), "The assigned shelter lacks a working lift."),
        PersonSeed("accessibility designer", "Fix the crossing", "Get the temporary route tested with the people who use it.", ("dignity", "evidence"), ("direct", "curious"), "The approved design omitted a ramp to meet a construction deadline."),
        PersonSeed("small supplier", "Win a public bid", "Submit a compliant offer without taking on unpayable credit.", ("independence", "craft"), ("ambitious", "cautious"), "Has been asked to underbid by a larger supplier that plans to buy them out."),
        PersonSeed("block captain", "Check on the neighbors", "Coordinate water and cooling visits during a heat warning.", ("community", "care"), ("warm", "persistent"), "Has not told the block that their own cooling unit is broken."),
        PersonSeed("public defender", "Keep a case fair", "Find a witness who can confirm the time-line without coaching them.", ("justice", "integrity"), ("sharp", "patient"), "The witness has already given a conflicting statement to a relative."),
        PersonSeed("data privacy officer", "Limit a shared record", "Allow a useful referral without passing unnecessary health data.", ("privacy", "care"), ("careful", "collaborative"), "A manager requested access outside their assigned role."),
        PersonSeed("union treasurer", "Balance the hardship fund", "Help a worker without exhausting funds promised to others.", ("solidarity", "accountability"), ("practical", "empathetic"), "The ledger includes an undocumented emergency loan to a friend."),
        PersonSeed("local journalist", "Correct a rumor", "Verify a viral claim before it changes how residents treat a neighbor.", ("truth", "harm reduction"), ("skeptical", "quick"), "Their outlet's sponsor benefits from the rumor being believed."),
        PersonSeed("transit mechanic", "Find the fault", "Diagnose a repeat brake issue before the train returns to service.", ("safety", "craft"), ("focused", "stubborn"), "The parts supplier has substituted a cheaper component."),
        PersonSeed("community mediator", "Restore a working pact", "Bring vendors and residents together after a noise complaint.", ("balance", "respect"), ("measured", "social"), "One side has already filed a formal complaint without telling the other."),
        PersonSeed("housing advocate", "Protect a tenant's access", "Find a workable route during the building's scheduled repairs.", ("dignity", "access"), ("persistent", "careful"), "A proposed temporary entrance is not accessible to a resident who uses a wheelchair."),
    ),
    extra_given_names=("Abena", "Adrian", "Aiko", "Amina", "Andres", "Anika", "Beatriz", "Caleb", "Camila", "Dae", "Elena", "Emil", "Farah", "Felix", "Gabriela", "Hana", "Imani", "Javier", "Kiran", "Leila", "Malik", "Maya", "Nikhil", "Olivia"),
    extra_family_names=("Alvarez", "Baker", "Castillo", "Davis", "Evans", "Flores", "Garcia", "Hughes", "Ito", "Johnson", "Kaur", "Lin", "Miller", "Nakamura", "Ortiz", "Parker", "Quinn", "Robinson", "Shah", "Taylor", "Ueda", "Valdez", "Wilson", "Young"),
    extra_roles=("nurse", "bus driver", "vendor", "teacher", "mechanic", "care worker", "barista", "student", "analyst", "repair technician", "law clerk", "dispatcher", "cook", "delivery rider", "bookkeeper", "designer", "security guard", "social worker", "warehouse associate", "librarian", "clinic receptionist", "electrician", "tenant advocate", "data operator"),
    settlement_names=("Central", "Southline", "Market", "Park", "Depot", "Landing", "Works", "Clinic", "Commons"),
    settlement_kinds=("residential_block", "transit_stop", "market_street", "public_park", "rail_depot", "ferry_landing", "workshop_district", "clinic_campus", "community_center"),
    themes=("public trust", "shift work", "housing", "health access", "infrastructure", "labor"),
    systems=("municipal_governance", "labor_markets", "public_transit", "health_networks", "civic_records", "multidimensional_relations"),
    scenario_hooks=("A late transit outage strands clinic staff and freight workers.", "A public works contract changes a tenant's access route.", "A viral accusation requires evidence before a neighbor is harmed."),
    discovery_site=DiscoverySiteSeed(
        name="Old Station Incident Cabinet",
        description=(
            "Behind a sealed maintenance panel, an archived incident sheet records the station outage "
            "before the public works repair order was approved. The initials identify a shift supervisor, not a culprit."
        ),
    ),
    rules={
        "magic": {"enabled": False, "reason": "This background contains no supernatural abilities."},
        "government": {"system": "representative_and_municipal", "due_process": True, "public_records": True},
        "economy": {"production": "contract_and_supply_chain", "currency": "modern.credit", "resource_origin_required": True},
        "combat": {"lethal_force": "rare_and_consequential", "injuries_persist": True, "deescalation_available": True},
    },
    regional_occupation_profiles={
        "Harbor Ward": RegionalOccupationProfile(
            role_weights={"vendor": 7, "mechanic": 6, "bus driver": 4},
            role_skill_levels={
                "vendor": {"skill.social": "72"},
                "mechanic": {"skill.repair": "76"},
                "bus driver": {"skill.navigation": "73"},
            },
        ),
        "North Station": RegionalOccupationProfile(
            role_weights={"bus driver": 9, "dispatcher": 8, "delivery rider": 5},
            role_skill_levels={
                "bus driver": {"skill.navigation": "78"},
                "dispatcher": {"skill.navigation": "72", "skill.research": "60"},
                "delivery rider": {"skill.navigation": "70"},
            },
        ),
        "Old Town": RegionalOccupationProfile(
            role_weights={"vendor": 8, "tenant advocate": 6, "designer": 4},
            role_skill_levels={
                "vendor": {"skill.social": "74"},
                "tenant advocate": {"skill.mediation": "76"},
                "designer": {"skill.research": "61"},
            },
        ),
        "University Quarter": RegionalOccupationProfile(
            role_weights={"nurse": 10, "care worker": 9, "clinic receptionist": 8, "data operator": 7},
            role_skill_levels={
                "nurse": {"skill.care": "82", "skill.research": "62"},
                "care worker": {"skill.care": "78", "skill.social": "68"},
                "clinic receptionist": {"skill.care": "68", "skill.social": "70"},
                "data operator": {"skill.research": "76"},
            },
        ),
        "East Works": RegionalOccupationProfile(
            role_weights={"mechanic": 9, "repair technician": 8, "electrician": 7, "warehouse associate": 4},
            role_skill_levels={
                "mechanic": {"skill.repair": "80"},
                "repair technician": {"skill.repair": "82"},
                "electrician": {"skill.repair": "78"},
                "warehouse associate": {"skill.local_knowledge": "62"},
            },
        ),
        "Civic Center": RegionalOccupationProfile(
            role_weights={"law clerk": 8, "social worker": 7, "analyst": 6, "data operator": 5},
            role_skill_levels={
                "law clerk": {"skill.research": "74", "skill.mediation": "63"},
                "social worker": {"skill.care": "72", "skill.mediation": "72"},
                "analyst": {"skill.research": "78"},
                "data operator": {"skill.research": "73"},
            },
        ),
        "South Gardens": RegionalOccupationProfile(
            role_weights={"teacher": 7, "nurse": 7, "care worker": 7, "vendor": 4},
            role_skill_levels={
                "teacher": {"skill.social": "73", "skill.research": "60"},
                "nurse": {"skill.care": "79"},
                "care worker": {"skill.care": "77"},
                "vendor": {"skill.social": "69"},
            },
        ),
        "West Exchange": RegionalOccupationProfile(
            role_weights={"analyst": 8, "bookkeeper": 7, "data operator": 6, "delivery rider": 4},
            role_skill_levels={
                "analyst": {"skill.research": "79"},
                "bookkeeper": {"skill.research": "68"},
                "data operator": {"skill.research": "74"},
                "delivery rider": {"skill.navigation": "66"},
            },
        ),
        "Riverline": RegionalOccupationProfile(
            role_weights={"bus driver": 7, "delivery rider": 7, "mechanic": 5, "vendor": 4},
            role_skill_levels={
                "bus driver": {"skill.navigation": "76"},
                "delivery rider": {"skill.navigation": "72"},
                "mechanic": {"skill.repair": "71"},
                "vendor": {"skill.social": "68"},
            },
        ),
        "Hillview": RegionalOccupationProfile(
            role_weights={"care worker": 8, "clinic receptionist": 6, "electrician": 5, "bus driver": 4},
            role_skill_levels={
                "care worker": {"skill.care": "78"},
                "clinic receptionist": {"skill.care": "65", "skill.social": "68"},
                "electrician": {"skill.repair": "70"},
                "bus driver": {"skill.navigation": "71"},
            },
        ),
        "Airport Loop": RegionalOccupationProfile(
            role_weights={"warehouse associate": 8, "dispatcher": 7, "security guard": 6, "delivery rider": 6},
            role_skill_levels={
                "warehouse associate": {"skill.local_knowledge": "72"},
                "dispatcher": {"skill.navigation": "73"},
                "security guard": {"skill.local_knowledge": "68", "skill.social": "60"},
                "delivery rider": {"skill.navigation": "74"},
            },
        ),
        "South Market": RegionalOccupationProfile(
            role_weights={"vendor": 9, "cook": 7, "bookkeeper": 5, "delivery rider": 4},
            role_skill_levels={
                "vendor": {"skill.social": "80"},
                "cook": {"skill.local_knowledge": "67"},
                "bookkeeper": {"skill.research": "64"},
                "delivery rider": {"skill.navigation": "66"},
            },
        ),
    },
)


CULTIVATION = BackgroundDefinition(
    pack_id="long_life_cultivation",
    version="1.7.0",
    title="The Long River of Qi",
    subtitle="Long-lived cultivation world",
    summary="A world of mortal valleys, long-lived cultivators, sect institutions and scarce spirit resources. Advancement has real time, health, material and relationship costs.",
    setting="The mountain sects are renegotiating access to a spirit river after a change in its flow. Cultivation is powerful but bounded by body, knowledge, resources, vows and political relationships. Long life preserves obligations across generations.",
    year_days=360,
    calendar_name="calendar.cultivator_year_360",
    currency_id="cultivation.spirit_stone",
    currency_name="spirit stones",
    resources=("cultivation.spirit_stone", "cultivation.qi", "cultivation.herbs", "cultivation.ore", "cultivation.grain", "cultivation.ink", "cultivation.medicine", "cultivation.spirit_water"),
    regions=(
        RegionSeed("Cloudstep Range", "High monasteries and cliff paths where weather governs travel.", "high_mountain", "ore", "thin-air exhaustion"),
        RegionSeed("Jade River Basin", "A spirit-water corridor shared by farms and cultivation estates.", "river_valley", "grain", "water-right disputes"),
        RegionSeed("Red Lantern Coast", "A chain of ports serving medicine traders and wandering schools.", "coastal_isles", "medicine", "sea storms"),
        RegionSeed("Quiet Pine Sea", "Old forests with herb terraces and guarded gathering routes.", "mountain_forest", "herbs", "overharvesting"),
        RegionSeed("Stone Meridian", "A mineral plateau where ore veins cross sect boundaries.", "mineral_plateau", "ore", "unstable tunnels"),
        RegionSeed("Hundred Bell Plain", "A broad plain of markets, courier stations and pilgrimage halls.", "open_plain", "grain", "festival crowding"),
        RegionSeed("Frostgate Peaks", "A cold pass connecting isolated lineages and monastery holds.", "alpine_pass", "ore", "snow closure"),
        RegionSeed("Silver Reed Marsh", "Wetlands where rare medicines depend on careful harvest cycles.", "herbal_wetland", "herbs", "habitat loss"),
        RegionSeed("Sunward Terrace", "A terraced farming region that supports several mountain schools.", "terraced_hills", "grain", "irrigation failures"),
        RegionSeed("Mirror Lake Domain", "A lake region known for debate halls and reflected-sky observatories.", "lake_basin", "ink", "shoreline claims"),
        RegionSeed("Black Anvil Ridge", "A craft corridor for tools, protective gear and measured refinements.", "forge_ridge", "ore", "forge accidents"),
        RegionSeed("Dawn Veil Valley", "A quiet valley of clinics, family compounds and old vows.", "sheltered_valley", "medicine", "succession pressure"),
    ),
    polities=(
        PolitySeed("Cloudstep Protectorate", "sect_council", "The mountain pass remains open to registered travelers.", "ore"),
        PolitySeed("Jade River Compact", "water_council", "Upstream cultivation cannot exceed the recorded seasonal draw.", "grain"),
        PolitySeed("Red Lantern Maritime Court", "port_council", "Cargo claims require a witnessed manifest.", "medicine"),
        PolitySeed("Quiet Pine Stewardship", "stewardship_council", "Rare herbs are harvested by a recovery schedule.", "herbs"),
        PolitySeed("Stone Meridian League", "charter_league", "Mine boundaries follow the surveyed meridian markers.", "ore"),
        PolitySeed("Hundred Bell Assembly", "market_assembly", "Festival markets publish fees before opening.", "grain"),
        PolitySeed("Frostgate March", "mountain_march", "Pass closure requires a declared weather hazard.", "ore"),
        PolitySeed("Silver Reed Conservancy", "ecological_council", "Medicine harvesting cannot deplete breeding wetlands.", "herbs"),
        PolitySeed("Sunward Terrace Federation", "village_federation", "Irrigation turns are allocated by the annual compact.", "grain"),
        PolitySeed("Mirror Lake Domain", "scholar_council", "Debate halls remain open to registered schools and residents.", "ink"),
        PolitySeed("Black Anvil Principality", "craft_principality", "Forge safety inspections precede large production runs.", "ore"),
        PolitySeed("Dawn Veil Household Council", "lineage_council", "Succession decisions require a public hearing among heirs.", "medicine"),
        PolitySeed("North Meridian Court", "sect_council", "Training grounds are shared under a posted schedule.", "ore"),
        PolitySeed("Eastern Rainwater League", "water_council", "Rain stores are reserved before commercial extraction.", "grain"),
        PolitySeed("Coral Gate Ports", "port_council", "A ship's crew receives its own cargo inventory copy.", "medicine"),
        PolitySeed("Pine Needle Canton", "forest_council", "Herb trails close during regrowth season.", "herbs"),
        PolitySeed("Five Veins Mining Council", "mine_charter", "Each active shaft funds a rescue and repair reserve.", "ore"),
        PolitySeed("Lotus Market Prefecture", "market_prefecture", "Medicine lots list harvest date and grade.", "medicine"),
        PolitySeed("Snow Gate Houses", "lineage_compact", "Travelers receive shelter when a pass closure is declared.", "grain"),
        PolitySeed("Reedbank Stewardship", "wetland_council", "Water channels remain connected through the dry season.", "herbs"),
        PolitySeed("Golden Stair Alliance", "village_federation", "Terrace repairs are shared by farms that use the channel.", "grain"),
        PolitySeed("Stillwater Scholarium", "scholar_domain", "Research findings distinguish observation from lineage claim.", "ink"),
        PolitySeed("Anvil March", "craft_march", "Protective gear is inspected before entering a high-risk forge.", "ore"),
        PolitySeed("Veil Valley Concord", "household_concord", "Care duties and succession claims are recorded separately.", "medicine"),
    ),
    organizations=(
        OrganizationSeed("Cloudstep Wayfarers", "courier_order", "Carry messages across high passes with arrival records.", "witnessed handoffs"),
        OrganizationSeed("Jade River Irrigation Guild", "water_cooperative", "Maintain channels and schedule seasonal water turns.", "measured draw"),
        OrganizationSeed("Red Lantern Medicine Exchange", "merchant_house", "Match medicine buyers with verified harvest lots.", "source and grade"),
        OrganizationSeed("Quiet Pine Herb Keepers", "stewardship_order", "Protect herb beds and teach sustainable gathering.", "regrowth periods"),
        OrganizationSeed("Stone Meridian Surveyors", "professional_guild", "Record mine boundaries and resolve survey disputes.", "replicable measures"),
        OrganizationSeed("Hundred Bell Courier Union", "courier_union", "Coordinate routes, lodging and fair delivery rates.", "sealed delivery"),
        OrganizationSeed("Frostgate Rescue Lodge", "rescue_order", "Recover travelers and maintain storm shelters.", "safety before toll"),
        OrganizationSeed("Silver Reed Medicine Hall", "medical_guild", "Treat illness and track side effects of rare remedies.", "observed outcomes"),
        OrganizationSeed("Sunward Terrace Granaries", "food_cooperative", "Store grain against drought and seed shortages.", "seed reserve first"),
        OrganizationSeed("Mirror Lake Record Hall", "scholar_guild", "Keep lineages, research and public debate records.", "claim provenance"),
        OrganizationSeed("Black Anvil Makers' Lodge", "craft_guild", "Train apprentices and standardize protective equipment.", "inspection before sale"),
        OrganizationSeed("Dawn Veil Healers", "care_network", "Provide care and coordinate family duty rosters.", "consent and rotation"),
        OrganizationSeed("North Meridian Training Circle", "training_order", "Share instructors and prevent unsafe practice.", "supervised training"),
        OrganizationSeed("Rainwater Cistern Keepers", "utility_cooperative", "Maintain cisterns and publish stored water levels.", "household minimum"),
        OrganizationSeed("Coral Gate Shipwrights", "craft_guild", "Repair boats and keep crew safety equipment maintained.", "documented inspection"),
        OrganizationSeed("Pine Needle Pharmacists", "medical_guild", "Prepare herbal medicines from traceable, mature harvests.", "batch records"),
        OrganizationSeed("Five Veins Miners' Lodge", "worker_lodge", "Share safety information and support injured miners.", "rescue reserve"),
        OrganizationSeed("Lotus Market Weighers", "market_guild", "Verify measures and resolve market lot disputes.", "open scales"),
        OrganizationSeed("Snow Gate Hostels", "hospitality_network", "Shelter travelers during storms and pass closures.", "posted shelter terms"),
        OrganizationSeed("Reedbank Water Stewards", "stewardship_group", "Protect wetlands and coordinate channel repair.", "habitat recovery"),
        OrganizationSeed("Golden Stair Farmers' Union", "labor_cooperative", "Coordinate terrace repairs and harvest labor.", "shared work ledger"),
        OrganizationSeed("Stillwater Debaters", "scholar_society", "Host public disputes about evidence and precedent.", "opposed claims heard"),
        OrganizationSeed("Anvil March Toolmakers", "craft_guild", "Supply tools and repair protective gear.", "repairable design"),
        OrganizationSeed("Veil Valley Family Registry", "record_office", "Keep separate records for care, inheritance and vows.", "distinct claims"),
    ),
    key_names=(
        ("Mei", "Lan"), ("Jian", "Wei"), ("Lian", "Zhao"), ("Tao", "Ren"),
        ("An", "Song"), ("Xue", "Lin"), ("Bo", "Shen"), ("Yun", "Qiao"),
        ("Rui", "Chen"), ("Hui", "Zhou"), ("Kai", "Wen"), ("Ning", "Luo"),
        ("Fen", "Guo"), ("Jun", "He"), ("Shan", "Xu"), ("Wei", "Tang"),
        ("Ming", "Deng"), ("Qin", "Feng"), ("Yao", "Gao"), ("Lei", "Mo"),
        ("Zhen", "Bai"), ("Xiao", "Du"), ("Yan", "Qin"), ("Chao", "Liu"),
        ("Jin", "Han"), ("Su", "Yu"), ("Rong", "Ma"), ("Ling", "Wu"),
        ("Yue", "Kang"), ("Bao", "Ning"), ("Lan", "Shen"), ("De", "Fang"),
    ),
    key_people=(
        PersonSeed("sect archivist", "Restore the river records", "Compare old water records before the next allocation vote.", ("accuracy", "continuity"), ("patient", "observant"), "A sealed page names a cultivator who is still alive."),
        PersonSeed("river cultivator", "Protect the downstream farms", "Limit extraction until the river returns to its seasonal course.", ("balance", "duty"), ("disciplined", "firm"), "Has been diverting a small flow to heal a former rival."),
        PersonSeed("medicine trader", "Verify a rare herb lot", "Trace the harvest location before accepting the buyer's price.", ("trade", "evidence"), ("charming", "skeptical"), "The herb's label names a protected wetland."),
        PersonSeed("herb steward", "Close the regrowth trail", "Convince senior students to pause gathering for one season.", ("stewardship", "patience"), ("gentle", "persistent"), "A respected master has been harvesting after the official closure."),
        PersonSeed("mine surveyor", "Resolve a vein boundary", "Re-measure the tunnel using a method both houses accept.", ("precision", "fairness"), ("methodical", "quiet"), "The old marker was moved before the latest survey."),
        PersonSeed("market courier", "Deliver a disputed contract", "Find the true recipient before the deadline expires.", ("reliability", "prudence"), ("quick", "wary"), "The letter's seal belongs to a retired office."),
        PersonSeed("pass rescuer", "Keep the route safe", "Replace an unsafe anchor before the snow returns.", ("safety", "service"), ("brave", "practical"), "A senior official ordered the damaged rope left in service."),
        PersonSeed("medicine hall physician", "Track a side effect", "Separate an herb reaction from a training injury.", ("care", "evidence"), ("cautious", "focused"), "A patient has hidden a second remedy they are taking."),
        PersonSeed("granary keeper", "Protect the seed stores", "Keep enough seed grain after a request to feed the mountain school.", ("stewardship", "care"), ("measured", "empathetic"), "The reserve count was copied incorrectly last winter."),
        PersonSeed("debate host", "Hear both lineage claims", "Set terms for a hearing that does not favor the older house.", ("fairness", "learning"), ("calm", "analytical"), "A close student has already promised a judgment to one claimant."),
        PersonSeed("forge inspector", "Stop unsafe refinement", "Require a cooling repair before the next high-output batch.", ("safety", "craft"), ("direct", "careful"), "The workshop's owner has hidden a near-miss report."),
        PersonSeed("family healer", "Share care without coercion", "Organize a duty roster that respects the patient's wishes.", ("care", "consent"), ("steady", "warm"), "A relative's care promise was made under pressure."),
        PersonSeed("training instructor", "Prevent a rushed breakthrough", "Help a student understand the body's recovery limits.", ("responsibility", "growth"), ("strict", "protective"), "The instructor once ignored the same warning and bears a lasting injury."),
        PersonSeed("cistern keeper", "Resolve a water shortfall", "Find a fair schedule before the dry-season reserve drops further.", ("fairness", "reliability"), ("practical", "blunt"), "A hidden cistern is supplying one lineage without public notice."),
        PersonSeed("shipwright", "Repair a storm-damaged hull", "Get the vessel seaworthy without using untested spirit-treated timber.", ("craft", "safety"), ("inventive", "skeptical"), "The owner has asked to conceal a hull crack from the crew."),
        PersonSeed("pharmacist", "Audit the herb batch", "Identify why two patients reacted differently to the medicine.", ("evidence", "care"), ("precise", "patient"), "The supplier mixed two harvests under one name."),
        PersonSeed("miners' speaker", "Secure a rescue fund", "Require mine owners to reserve resources before profits are shared.", ("solidarity", "safety"), ("forceful", "loyal"), "The speaker has accepted a private gift from one mine owner."),
        PersonSeed("market weigher", "Settle the stone measure", "Show whether a seller's scale is inaccurate or merely old.", ("honesty", "fairness"), ("exact", "curious"), "The same scale was approved by a respected predecessor."),
        PersonSeed("hostel keeper", "Shelter a stranded party", "Make room for travelers without evicting long-term guests.", ("hospitality", "balance"), ("generous", "firm"), "One traveler is hiding from a lawful but unfair debt claim."),
        PersonSeed("wetland steward", "Restore the reed beds", "Reopen a channel and stop medicine harvesting until it recovers.", ("stewardship", "care"), ("observant", "resolute"), "The first damage came from a project the steward approved."),
        PersonSeed("terrace farmer", "Protect the irrigation turn", "Keep the water compact from being changed after planting.", ("fairness", "family"), ("practical", "persistent"), "Has been lending water to a neighbor outside the compact."),
        PersonSeed("public debater", "Test a breakthrough claim", "Ask for evidence without insulting the claimant's lineage.", ("truth", "respect"), ("sharp", "measured"), "The debater privately knows the claim could be genuine."),
        PersonSeed("toolmaker", "Replace a brittle blade", "Find a safer material before miners return to the tunnel.", ("craft", "safety"), ("innovative", "restless"), "The alloy recipe came from a rival school under an old vow."),
        PersonSeed("registry keeper", "Separate care and inheritance", "Correct a document that merges two different family promises.", ("accuracy", "justice"), ("careful", "independent"), "A senior family member asked the keeper to leave the error in place."),
        PersonSeed("wandering disciple", "Return a borrowed manual", "Find its owner without exposing a private teaching dispute.", ("honor", "curiosity"), ("restless", "polite"), "Has copied one chapter before realizing it was restricted."),
        PersonSeed("valley mediator", "Renew a boundary accord", "Keep a mineral claim from breaking the seasonal farming compact.", ("balance", "peace"), ("diplomatic", "patient"), "The mediator's family benefits from the current boundary."),
        PersonSeed("long-lived gardener", "Repair an old grove", "Replant trees whose roots were damaged by a new path.", ("continuity", "stewardship"), ("slow", "kind"), "Remembers a previous agreement that no living council member believes."),
        PersonSeed("young cultivator", "Earn a safe training place", "Show skill without rushing into a dangerous challenge.", ("growth", "belonging"), ("ambitious", "anxious"), "A relative has promised the student to another sect."),
        PersonSeed("mountain courier", "Confirm an arrival", "Deliver proof of a message's handoff before the council closes.", ("reliability", "honor"), ("quick", "precise"), "The message was opened once during a storm."),
        PersonSeed("sect cook", "Feed the visiting students", "Stretch the store without hiding shortages from the kitchen crew.", ("care", "honesty"), ("resourceful", "social"), "Has been trading their own ration for a student's medicine."),
        PersonSeed("craft master", "Pass on a repair technique", "Teach a successor while the workshop is still operating.", ("legacy", "craft"), ("reserved", "generous"), "The named successor is not the master's preferred apprentice."),
        PersonSeed("retired guard", "Settle an old debt", "Repay a family whose help was never recorded in the official ledger.", ("honor", "gratitude"), ("quiet", "steadfast"), "The debt is tied to an incident the guard was ordered to conceal."),
    ),
    extra_given_names=("Ai", "Bao", "Cai", "Dan", "En", "Fang", "Gu", "Hao", "Jing", "Kai", "Luo", "Min", "Na", "Ou", "Ping", "Qiu", "Shu", "Tian", "Wen", "Xin", "Ying", "Zhi", "Yun", "Zhen"),
    extra_family_names=("An", "Bai", "Cen", "Duan", "Fan", "Gao", "He", "Jia", "Kong", "Lei", "Mu", "Ning", "Pei", "Qiao", "Ruan", "Su", "Tan", "Wan", "Xie", "Yao", "Zeng", "Zhou", "Luo", "Shen"),
    extra_roles=("valley farmer", "disciple", "courier", "herb gatherer", "boat hand", "scribe", "forge worker", "healer", "market vendor", "water steward", "hostel keeper", "cook", "apprentice", "porter", "teacher", "weaver", "miner", "boatwright", "record keeper", "watch guard", "granary worker", "gardener", "traveler", "pharmacist"),
    settlement_names=("Cloud Gate", "Riverbend", "Lantern", "Pine Spring", "Stone Market", "Bell Court", "Frost Step", "Reed House", "Sun Terrace"),
    settlement_kinds=("mountain_hamlet", "river_market", "port_village", "herb_station", "mine_town", "pilgrim_market", "pass_shelter", "wetland_village", "terrace_settlement"),
    themes=("long memory", "vow and consent", "scarce medicine", "water rights", "sect succession", "measured advancement"),
    systems=("long_life", "cultivation_progression", "sect_governance", "spirit_resource_economy", "herb_regrowth", "multidimensional_relations"),
    scenario_hooks=("A spirit river flow changes before the annual water compact.", "A disputed medicine batch harms one student but helps another.", "A succession hearing must separate vows, care duty and inheritance."),
    discovery_site=DiscoverySiteSeed(
        name="Quiet Spring Measure Stone",
        description=(
            "A weathered measure stone concealed beside an old spring records a seasonal flow boundary. "
            "Its marks predate the present compact and show that the disputed diversion began before either lineage's claim."
        ),
    ),
    rules={
        "magic": {"enabled": True, "system": "cultivation", "resource": "cultivation.qi", "advancement_costs": ["time", "health", "materials", "social obligations"]},
        "government": {"succession": "sect_and_lineage_rules", "water_rights": "seasonal_compacts", "knowledge_access": "lineage_and_observation"},
        "economy": {"production": "agriculture_craft_and_gathering", "currency": "cultivation.spirit_stone", "resource_origin_required": True},
        "combat": {"lethal_force": "possible_and_costly", "injuries_persist": True, "retreat_and_negotiation_allowed": True},
        "longevity": {"ordinary_lifespan_years": 100, "cultivator_lifespan_years": 1200, "death_releases_obligations": True},
    },
)


BACKGROUNDS: tuple[BackgroundDefinition, ...] = (LOW_MAGIC, MODERN_CITY, CULTIVATION)
