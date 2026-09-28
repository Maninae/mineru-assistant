#!/bin/bash
# Weather with fallback: wttr.in (5s timeout) → Open-Meteo
#
# All location inputs are env-overridable. With no location set, wttr.in
# geolocates by IP; the Open-Meteo fallback only runs when LAT/LON are given.
LOCATION="${MINERU_WEATHER_LOCATION:-}"
LAT="${MINERU_WEATHER_LAT:-}"
LON="${MINERU_WEATHER_LON:-}"
LABEL="${MINERU_WEATHER_LABEL:-Weather}"

# Try wttr.in first (5 second timeout)
result=$(curl -s --max-time 5 "wttr.in/${LOCATION}?format=%c+%t+(%C)" 2>/dev/null)

if [[ -n "$result" && "$result" != *"Unknown"* && "$result" != *"error"* ]]; then
    echo "$LABEL: $result"
    exit 0
fi

# Fallback to Open-Meteo (needs explicit coordinates)
if [[ -n "$LAT" && -n "$LON" ]]; then
    json=$(curl -s --max-time 10 "https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&current_weather=true&temperature_unit=fahrenheit")

    if [[ -n "$json" ]]; then
        temp=$(echo "$json" | jq -r '.current_weather.temperature')
        code=$(echo "$json" | jq -r '.current_weather.weathercode')

        # WMO weather codes → description
        case $code in
            0) desc="Clear" ;;
            1|2|3) desc="Partly cloudy" ;;
            45|48) desc="Foggy" ;;
            51|53|55|61|63|65|80|81|82) desc="Rainy" ;;
            71|73|75|77|85|86) desc="Snowy" ;;
            95|96|99) desc="Thunderstorm" ;;
            *) desc="Weather code $code" ;;
        esac

        echo "$LABEL: ${temp}°F, ${desc}"
        exit 0
    fi
fi

echo "Weather unavailable"
exit 1
