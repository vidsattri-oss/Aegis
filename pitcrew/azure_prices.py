"""Import exact Azure consumption token meters without account credentials."""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from .contracts import require
from .integrations import request
from .usage import usd_amount


METER_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z", re.I)
INPUT = re.compile(r"\b(?:input|inp)\b", re.I)
OUTPUT = re.compile(r"\b(?:output|outp)\b", re.I)


def azure_token_rates(region, input_meter, output_meter):
    require(isinstance(region, str) and re.fullmatch(r"[a-z0-9]{2,36}", region), "Enter an Azure region code such as eastus")
    require(all(isinstance(raw, str) and METER_ID.fullmatch(raw) for raw in (input_meter, output_meter)),
            "Enter the Azure input and output meter IDs")
    incoming, outgoing = input_meter.lower(), output_meter.lower()
    require(incoming != outgoing, "Choose different input and output meters")
    filters = (f"armRegionName eq '{region}' and priceType eq 'Consumption' "
               f"and (meterId eq '{incoming}' or meterId eq '{outgoing}')")
    query = urlencode({"$filter": filters, "currencyCode": "'USD'", "api-version": "2023-01-01-preview"})
    data, _ = request("https://prices.azure.com/api/retail/prices?" + query, timeout=15, limit=700_000, public=True)
    response = json.loads(data)
    require(isinstance(response, dict) and isinstance(response.get("Items"), list) and not response.get("NextPageLink"),
            "Azure did not return a complete token price result for those meters")

    def select(identifier, direction):
        rows = []
        today = datetime.now(timezone.utc).date().isoformat()
        for item in response["Items"]:
            name = item.get("meterName", "")
            sku = item.get("skuName", "")
            rate = usd_amount(item.get("retailPrice"))
            units = {"1K": 1_000, "1M": 1_000_000}.get(item.get("unitOfMeasure"))
            start = item.get("effectiveStartDate", "")
            if (item.get("meterId", "").lower() == identifier and item.get("armRegionName") == region
                    and item.get("currencyCode") == "USD" and item.get("type") == "Consumption"
                    and item.get("tierMinimumUnits") == 0 and rate is not None and units and sku
                    and direction.search(sku) and re.search(r"\btokens?\b", name, re.I)
                    and not re.search(r"\b(?:cached|batch|training)\b", sku, re.I) and start[:10] <= today):
                rows.append((start, rate * 1_000_000 / units, sku, name))
        require(rows, "Azure could not verify a current uncached input or output token rate for the selected meter")
        current = max(row[0] for row in rows)
        matches = {row[1:] for row in rows if row[0] == current}
        require(len(matches) == 1, "Azure returned conflicting prices for the selected meter")
        return matches.pop()

    input_rate, input_sku, input_name = select(incoming, INPUT)
    output_rate, output_sku, output_name = select(outgoing, OUTPUT)
    require(INPUT.sub("", input_sku.casefold()).split() == OUTPUT.sub("", output_sku.casefold()).split(),
            "The input and output meters must cover the same model and deployment SKU")
    return {"input_rate": input_rate, "output_rate": output_rate, "rate_source": "azure_retail", "region": region,
            "input_meter": input_name, "output_meter": output_name}
