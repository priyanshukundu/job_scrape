"""locations_exclude: non-US sites stay out, US sites (including ones that say Headquarters) stay in."""
import json
from pathlib import Path

import pytest

import job_scout as js

CFG = json.loads((Path(__file__).parent / "data" / "config.json").read_text(encoding="utf-8"))["filters"]
FILTERS = {"title_include": [], "title_exclude": [], "locations_include": [], "locations_exclude": CFG["locations_exclude"]}


def kept(loc):
    return js.matches({"title": "x", "location": loc}, FILTERS)


EXCLUDED = [
    "PH", "MX", "SG", "Headquarters", "Central Region (City Area)", "One Island East", "Kwun Tong, HK", "Central, HK",
    "Monterrey, NLE, MX", "Citi Center - MX", "Bukit Jalil KL, MY", "Schweiz - Nordschweiz", "Lodz, PL, 93-281",
    "Las Condes, RM, CL", "Port Moresby, PG", "Ramat-Gan, ISR", "ED3 - 20 Brandon Street, Edinburgh",
    "Whitby, Ontario", "Pasig - 4th Floor JMT Corporate Condominium", "Delhi NCR", "Munich - Müllerstrasse", "Fortitude Valley, QLD, au", "Ludhiana", "Kaohsiung", "Camana Bay", "Tunis", "København S, DK, 2300", "Saint Helier, Jersey", "Newcastle, NSW, au", "Ontario Home Office", "Perth Office", "Guangzhou", "Gandhi Nagar - GIFT City",
    "Perth, WA, au", "Docklands, AU", "1/124, SHIVAJI GARDENS, MOONLI", "Thane, IN", "AU - BRISBANE - 50 JAMES ST", "Perth - 225 St. Georges Terrace", "Norwich - Willow", "Parma - Via Carlo Pisacane, 1B", "Łódź, PL, 93-281", "Belfast - Merchant Square (IE)", "Belfast - 20 Adelaide Street",
    "Heredia, Provincia de Heredia", "Boadilla del Monte", "2 Locations (MesseTurm DEFAMESS HQ)", "Købanhavn K, DK, 1402", "3 Locations (Zrich)", "5 Locations (Canadian Head Office)", "2 Locations (POL Gdynia 3T Office Park Tower C)", "2 Locations (Lvis)", "Kulim, Kedah, al", "Milton Keynes", "CD - Kinshasa, Democratic Republic of Congo", "Northampton, Barclays Campus, Pavilion Drive", "Henley-on-Thames, Oxfordshire", "Ranjangaon", "2 Locations (9 10 TAUNUSANLAGE FRANKFURT AM MAIN)", "1054, Retiro, Capital Federal", "Kolhapur", "Jersey, JE", "Brunei, BN", "Addlestone", "Calvin Klein Halfweg Sugar City", "Birmingham, One Snow Hill", "Myanmar, MM - AIA Myanmar", "Pathum Tani", "I-Think Techno, Kanjurmarg", "PSA - Tandag City",
    "Phnom Penh, KH-AIA Cambodia", "Sumida-ku", "National General Tijuana", "OSL Airport", "Hosur", "BEL DIEG 6A", "Macau, Macau, Macau", "CH-xx-Le Locle-191-Facility", "HKG | Tuen Mun | No. 3, Kin Tai Street", "Bhiwandi, Maharashtra", "Uzhhorod", "Baja", "Mezzovico", "Croatia, Rijeka, 1351000", "NFS AU BRISBANE AIRPORT", "CK Blanchardstown", "LAN - Manchester", "Calvin Klein Manchester", "KOBE GO & IC - SANNOMIYA", "IloIlo", "Lviv", "Palma de Mallorca", "Trento", "STATION STREET:NOTTINGHAM", "Taytay CSC", "PH-BTG-TANAUAN CITY-BQ4 ~ First Philippine Industrial Park", "FRASER VALLEY", "KITCHENER WATERLOO", "NORTH YORK", "OTTAWA", "CDMX", "Lisboa", "Nassau", "Surrey", "San Sebastian", "San Jos�",
    "Quito", "Wien - Donau-City-Straße 7", "Helsinki, FI, 00500", "Mönchengladbach - Santander-Platz", "PGX Porto Alegre", "2 CENTRAL SQUARE:CARDIFF", "Citynet - Mandaluyong City", "Visakhapatnam, AP", "Managua, Nicaragua", "PSA Isabela", "PSA Aurora",
    "Adelaide, SA, AU, 5000", "Diegem, BE, 1831", "CABA, B, AR, 1001", "Hamburg, HH, DE, 20148",
    "Wellington, nz", "Adelaide, AU", "BA - Rio de Janeiro",
    "Aarhus", "Algiers, Algeria", "Andorra", "Basildon, Endeavour Drive", "Bettlach", "Bracknell", "Brno, Czechia", "Caracas, Venezuela", "Cruiserath - IE", "Cumbria", "Diego Garcia, British Indian Ocean Territory", "Duesseldorf", "Guayaquil", "Nicosia, CY", "Limassol - City House", "Louvain-L-N Boulevard Baudouin", "Montevideo, Uruguay", "Roma - Largo Fochetti 29", "Iskandar Puteri, 01, MY, 79250", "SRB BELG 136B FLR2", "3400, CORRIENTES, Corrientes", "WOODWATER PARK, PYNES HILL:EXETER",
    "Baku", "St Helier, Jersey, Jersey", "1401-GIPL: Incubation Cntr, Hanamkonda, Telangana", "Las Pinas (PHL)", "Lysaker", "Sucursal Holanda", "138_Servicios Centrales RV", "JUAN IGNACIO LUCA DE TENA",
]
US_KEPT = [
    "Perth Amboy, NJ", "Delhi, NY", "Delhi, LA", "Monterey, CA", "Indianapolis, IN", "Cologne, MN",
    "IN", "DE", "CA", "ID", "AR", "Indianapolis, IN",
    "US-NJ-Princeton-100-Headquarters", "Office - USA - CA - Headquarters", "New York, NY", "Ontario, CA",
    "Houston Office", "Jersey City", "Charlotte", "Boston, Massachusetts", "San Francisco Office",
    "Washington D.C.", "Montclair, NJ", "Nashville, Tennessee", "San Juan, Puerto Rico", "Cameron", "Wilmington, Delaware", "Perth Amboy, NJ", "Nassau County, NY", "Brisbane, CA", "Belfast, ME", "Delhi, NY", "Medina, OH",
    "Aurora CO", "Florence, KY", "Cheshire, CT", "Genoa, OH",
    "San Jose", "SAN JOSE", "Ottawa, IL", "Manchester, NH", "San Sebastian, Puerto Rico", "Pembroke", "New York, NY Calvin Klein Office",
    "New York, NY, US, 10001-8604", "Chicago, IL, US, 60606", "Fort Wayne, IN, 46802", "Wilmington, DE, 19801", "Dallas, TX, 75201-1234", "Nashville, TN", "Newark, DE", "Boston, MA",
]


@pytest.mark.parametrize("loc", EXCLUDED)
def test_non_us_locations_excluded(loc):
    assert not kept(loc)


@pytest.mark.parametrize("loc", US_KEPT)
def test_us_locations_kept(loc):
    assert kept(loc)


def test_drop_location_filtered_removes_only_non_us_new_rows():
    queue = [
        {"id": "a", "state": "new", "location": "Kwun Tong"},
        {"id": "b", "state": "new", "location": "US-NJ-Princeton-100-Headquarters"},
        {"id": "c", "state": "applied", "location": "Kwun Tong"},
    ]
    counts = {}
    js._drop_location_filtered(queue, CFG, counts)
    assert [e["id"] for e in queue] == ["b", "c"]
    assert counts["location_dropped"] == 1


def test_blank_location_recovers_site_from_link_or_title():
    fid = {"title": "Credit Analyst", "location": "", "link": "https://fil.wd3.myworkdayjobs.com/001/job/Gurgaon-Office/Credit-Analyst_J1"}
    mod = {"title": "Associate", "location": "", "link": "https://modernatx.wd1.myworkdayjobs.com/M_tx/job/Warsaw---Poland/Associate_R1"}
    ey = {"title": "Consultant (Eschborn / Frankfurt (Main), HE, DE, 65760)", "location": "", "link": "https://careers.ey.com/ey/job/x/1/"}
    us = {"title": "Associate", "location": "", "link": "https://modernatx.wd1.myworkdayjobs.com/M_tx/job/Cambridge-Massachusetts/Associate_R2"}
    for job in (fid, mod, ey):
        assert not js.matches(job, FILTERS)
    assert js.matches(us, FILTERS)


def test_foreign_cities_without_country_are_excluded_2026_10_20():
    for loc in ("Maroussi", "Zug", "Ulaanbaatar", "Bordeaux", "Valencia; Murcia; Alicante",
                "Golflinks Software Park Private Limited", "Vikram Monarch",
                "Liverpool Office Hub (GBLIVROY)", "Genève/ Rue du Rhône 31"):
        assert not js.matches({"title": "Associate", "location": loc, "link": ""}, FILTERS)
