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


# Onboarding allows at most 4 questions with at most 50 answers each, so the five regions are asked in
# three country questions (the fourth is for interests). A group that doesn't fit lists every country
# with members, then the most populous, and the rest use "Somewhere else in <region>".
ONBOARDING_GROUPS = [
    ("Europe", ["Europe"]),
    ("Middle East & Asia-Pacific", ["Middle East", "Asia-Pacific"]),
    ("Americas & Africa", ["Americas", "Africa"]),
]

# Most populous first, for choosing which countries fit in a full onboarding question.
POPULATION_ORDER = [
    "United States", "Brazil", "Nigeria", "Mexico", "Ethiopia", "Egypt", "DR Congo", "Tanzania",
    "South Africa", "Kenya", "Colombia", "Sudan", "Argentina", "Uganda", "Algeria", "Canada", "Peru",
    "Morocco", "Angola", "Mozambique", "Ghana", "Venezuela", "Madagascar", "Ivory Coast", "Cameroon",
    "Niger", "Chile", "Mali", "Burkina Faso", "Malawi", "Guatemala", "Ecuador", "Zambia", "Chad",
    "Somalia", "Senegal", "Zimbabwe", "Guinea", "Bolivia", "Rwanda", "Benin", "Burundi", "Haiti",
    "Tunisia", "Dominican Republic", "South Sudan", "Cuba", "Honduras", "Togo", "Sierra Leone",
    "Paraguay", "Nicaragua", "Libya", "El Salvador", "Republic of the Congo", "Liberia", "Costa Rica",
    "Mauritania", "Panama", "Uruguay", "Eritrea", "Namibia", "Jamaica", "Gambia", "Gabon", "Botswana",
    "Puerto Rico", "Lesotho", "Trinidad and Tobago", "Mauritius", "Guyana", "Suriname", "Cape Verde",
    "Bahamas", "Belize", "Barbados",
]

# The country's name in its own language(s), shown under the English name in onboarding when the
# old option had no description of its own. Left out where it's the same as the English name.
ENDONYMS = {
    "Albania": "Shqipëria", "Armenia": "Հայաստան", "Austria": "Österreich", "Azerbaijan": "Azərbaycan",
    "Belarus": "Беларусь", "Belgium": "België / Belgique / Belgien", "Bosnia and Herzegovina": "Bosna i Hercegovina",
    "Bulgaria": "България", "Croatia": "Hrvatska", "Cyprus": "Κύπρος / Kıbrıs", "Czechia": "Česko",
    "Denmark": "Danmark", "Estonia": "Eesti", "Finland": "Suomi", "Georgia": "საქართველო",
    "Germany": "Deutschland", "Greece": "Ελλάδα", "Hungary": "Magyarország", "Iceland": "Ísland",
    "Ireland": "Éire", "Italy": "Italia", "Kosovo": "Kosova", "Latvia": "Latvija", "Lithuania": "Lietuva",
    "Luxembourg": "Lëtzebuerg", "Montenegro": "Crna Gora", "Netherlands": "Nederland",
    "North Macedonia": "Северна Македонија", "Norway": "Norge", "Poland": "Polska", "Romania": "România",
    "Russia": "Россия", "Serbia": "Србија", "Slovakia": "Slovensko", "Slovenia": "Slovenija",
    "Spain": "España", "Sweden": "Sverige", "Switzerland": "Schweiz / Suisse / Svizzera",
    "Ukraine": "Україна",
    "Brazil": "Brasil", "Dominican Republic": "República Dominicana", "Haiti": "Haïti / Ayiti",
    "Mexico": "México", "Panama": "Panamá", "Paraguay": "Paraguái", "Peru": "Perú",
    "Algeria": "الجزائر", "Benin": "Bénin", "Burundi": "Uburundi", "Cameroon": "Cameroun",
    "Cape Verde": "Cabo Verde", "Chad": "Tchad / تشاد", "DR Congo": "RD Congo", "Republic of the Congo": "Congo",
    "Egypt": "مصر", "Eritrea": "ኤርትራ", "Ethiopia": "ኢትዮጵያ", "Guinea": "Guinée",
    "Ivory Coast": "Côte d'Ivoire", "Libya": "ليبيا", "Madagascar": "Madagasikara",
    "Mauritania": "موريتانيا", "Mauritius": "Maurice", "Morocco": "المغرب", "Mozambique": "Moçambique",
    "Senegal": "Sénégal", "Somalia": "Soomaaliya", "Sudan": "السودان", "Tunisia": "تونس",
    "Bahrain": "البحرين", "Iran": "ایران", "Iraq": "العراق", "Israel": "ישראל", "Jordan": "الأردن",
    "Kuwait": "الكويت", "Lebanon": "لبنان", "Oman": "عُمان", "Palestine": "فلسطين", "Qatar": "قطر",
    "Saudi Arabia": "السعودية", "Syria": "سوريا", "Turkey": "Türkiye", "United Arab Emirates": "الإمارات",
    "Yemen": "اليمن",
    "Afghanistan": "افغانستان", "Bangladesh": "বাংলাদেশ", "Bhutan": "འབྲུག", "Cambodia": "កម្ពុជា",
    "China": "中国", "Fiji": "Viti", "Hong Kong": "香港", "India": "भारत", "Japan": "日本",
    "Kazakhstan": "Қазақстан", "Kyrgyzstan": "Кыргызстан", "Laos": "ລາວ", "Maldives": "ދިވެހިރާއްޖެ",
    "Mongolia": "Монгол Улс", "Myanmar": "မြန်မာ", "Nepal": "नेपाल", "New Zealand": "Aotearoa",
    "Pakistan": "پاکستان", "Papua New Guinea": "Papua Niugini", "Philippines": "Pilipinas",
    "South Korea": "대한민국", "Sri Lanka": "ශ්‍රී ලංකාව", "Taiwan": "臺灣", "Tajikistan": "Тоҷикистон",
    "Thailand": "ประเทศไทย", "Turkmenistan": "Türkmenistan", "Uzbekistan": "Oʻzbekiston", "Vietnam": "Việt Nam",
}
