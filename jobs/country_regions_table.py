"""Which region each country role belongs to, for jobs/country_regions.py.

Keys are country role names exactly as they appear in the server (after the script renames
'Dominian Republic'). Values are the ISO 3166 alpha-2 code, used only for the flag emoji on a
new onboarding option. A role not listed here isn't treated as a country (e.g. 'California').

Regions as agreed on 21-22 Nov 2026:
- Europe includes Russia and the Caucasus (Armenia, Azerbaijan, Georgia)
- Africa includes Egypt and the Maghreb
- Middle East includes Turkey
- Asia-Pacific includes Central Asia
"""

REGIONS = ["Europe", "Americas", "Africa", "Middle East", "Asia-Pacific"]

COUNTRIES = {
    "Europe": {
        "Albania": "AL", "Andorra": "AD", "Armenia": "AM", "Austria": "AT", "Azerbaijan": "AZ",
        "Belarus": "BY", "Belgium": "BE", "Bosnia and Herzegovina": "BA", "Bulgaria": "BG",
        "Croatia": "HR", "Cyprus": "CY", "Czechia": "CZ", "Denmark": "DK", "Estonia": "EE",
        "Finland": "FI", "France": "FR", "Georgia": "GE", "Germany": "DE", "Greece": "GR",
        "Hungary": "HU", "Iceland": "IS", "Ireland": "IE", "Italy": "IT", "Kosovo": "XK",
        "Latvia": "LV", "Liechtenstein": "LI", "Lithuania": "LT", "Luxembourg": "LU", "Malta": "MT",
        "Moldova": "MD", "Monaco": "MC", "Montenegro": "ME", "Netherlands": "NL",
        "North Macedonia": "MK", "Norway": "NO", "Poland": "PL", "Portugal": "PT", "Romania": "RO",
        "Russia": "RU", "San Marino": "SM", "Serbia": "RS", "Slovakia": "SK", "Slovenia": "SI",
        "Spain": "ES", "Sweden": "SE", "Switzerland": "CH", "Ukraine": "UA",
        "United Kingdom": "GB",
    },
    "Americas": {
        "Argentina": "AR", "Bahamas": "BS", "Barbados": "BB", "Belize": "BZ", "Bolivia": "BO",
        "Brazil": "BR", "Canada": "CA", "Chile": "CL", "Colombia": "CO", "Costa Rica": "CR",
        "Cuba": "CU", "Dominican Republic": "DO", "Ecuador": "EC", "El Salvador": "SV",
        "Guatemala": "GT", "Guyana": "GY", "Haiti": "HT", "Honduras": "HN", "Jamaica": "JM",
        "Mexico": "MX", "Nicaragua": "NI", "Panama": "PA", "Paraguay": "PY", "Peru": "PE",
        "Puerto Rico": "PR", "Suriname": "SR", "Trinidad and Tobago": "TT",
        "United States": "US", "Uruguay": "UY", "Venezuela": "VE",
    },
    "Africa": {
        "Algeria": "DZ", "Angola": "AO", "Benin": "BJ", "Botswana": "BW", "Burkina Faso": "BF",
        "Burundi": "BI", "Cameroon": "CM", "Cape Verde": "CV", "Chad": "TD", "DR Congo": "CD",
        "Republic of the Congo": "CG", "Egypt": "EG", "Eritrea": "ER", "Ethiopia": "ET",
        "Gabon": "GA", "Gambia": "GM", "Ghana": "GH", "Guinea": "GN", "Ivory Coast": "CI",
        "Kenya": "KE", "Lesotho": "LS", "Liberia": "LR", "Libya": "LY", "Madagascar": "MG",
        "Malawi": "MW", "Mali": "ML", "Mauritania": "MR", "Mauritius": "MU", "Morocco": "MA",
        "Mozambique": "MZ", "Namibia": "NA", "Niger": "NE", "Nigeria": "NG", "Rwanda": "RW",
        "Senegal": "SN", "Sierra Leone": "SL", "Somalia": "SO", "South Africa": "ZA",
        "South Sudan": "SS", "Sudan": "SD", "Tanzania": "TZ", "Togo": "TG", "Tunisia": "TN",
        "Uganda": "UG", "Zambia": "ZM", "Zimbabwe": "ZW",
    },
    "Middle East": {
        "Bahrain": "BH", "Iran": "IR", "Iraq": "IQ", "Israel": "IL", "Jordan": "JO",
        "Kuwait": "KW", "Lebanon": "LB", "Oman": "OM", "Palestine": "PS", "Qatar": "QA",
        "Saudi Arabia": "SA", "Syria": "SY", "Turkey": "TR", "United Arab Emirates": "AE",
        "Yemen": "YE",
    },
    "Asia-Pacific": {
        "Afghanistan": "AF", "Australia": "AU", "Bangladesh": "BD", "Bhutan": "BT",
        "Cambodia": "KH", "China": "CN", "Fiji": "FJ", "Hong Kong": "HK", "India": "IN",
        "Indonesia": "ID", "Japan": "JP", "Kazakhstan": "KZ", "Kyrgyzstan": "KG", "Laos": "LA",
        "Malaysia": "MY", "Maldives": "MV", "Mongolia": "MN", "Myanmar": "MM", "Nepal": "NP",
        "New Zealand": "NZ", "Pakistan": "PK", "Papua New Guinea": "PG", "Philippines": "PH",
        "Singapore": "SG", "South Korea": "KR", "Sri Lanka": "LK", "Taiwan": "TW",
        "Tajikistan": "TJ", "Thailand": "TH", "Turkmenistan": "TM", "Uzbekistan": "UZ",
        "Vietnam": "VN",
    },
}

# Channels in the old country categories that aren't a country role's channel.
EXTRA_CHANNELS = {"european-union": "Europe"}

# Countries whose channels keep their own permission overwrites instead of syncing to the category.
KEEP_OWN_OVERWRITES = {"france", "germany"}


def region_of(country):
    for region, countries in COUNTRIES.items():
        if country in countries:
            return region
    return None
