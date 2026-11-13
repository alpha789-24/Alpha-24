# IIFL Capital Trading API Reference

Source: IIFL Capital developer documentation, transcribed from the text the project owner pasted. Sections marked "Notes (not from IIFL)" are reviewer notes. Omitted from the source: SDK step-by-step code samples, video links, the worked hex-to-decimal example.

All REST APIs use header: `Authorization: Bearer <userSession>`
Base URL for trading APIs: `https://api.iiflcapital.com/v1` (taken from the contract-file links; confirm in the portal).
`<userSession>` is the token returned by the getUserSession API (daily login, a new token each day).

---

## 1. Order Management

| Method | Endpoint | Mode | Action |
|---|---|---|---|
| POST | /orders | Single/Bulk | Place new orders |
| PUT | /orders/{brokerOrderId} | Single | Modify pending orders |
| DELETE | /orders/{brokerOrderId} | Single | Cancel a pending order |
| GET | /orders | Single | All orders placed today |
| GET | /orders/{brokerOrderId} | Single | History of a specific order |
| GET | /orders/lite/{brokerOrderId} | Single | Latest history object of a specific order |
| GET | /trades | Single | All executed trades today |

Notes from the source:
- All commodity segments (MCXCOMM, NSECOMM, BSECOMM, NCDEXCOMM) accept quantity in **lots** in Order and Margin APIs. All other segments accept **absolute quantity**.
- NCDEXCOMM accepts the tradingSymbol as instrumentId (e.g. `DHANIYA10JUL25PE8600FJUL25`). All other segments use the numeric instrument token (e.g. `2885` for NSEEQ).

### 1.1 Place Order: POST /orders

Request (JSON array):
```json
[
  {
    "instrumentId": "1594",
    "exchange": "NSEEQ",
    "transactionType": "BUY",
    "quantity": "1",
    "orderComplexity": "REGULAR",
    "product": "INTRADAY",
    "orderType": "MARKET",
    "validity": "DAY",
    "price": "1501.55",
    "slTriggerPrice": "1505.2",
    "slLegPrice": "1500",
    "targetLegPrice": "1600",
    "disclosedQuantity": "500",
    "marketProtectionPercent": "1400",
    "apiOrderSource": "XYZ",
    "algoId": "1234",
    "orderTag": "Stangle Leg 1"
  }
]
```

| Parameter | Type | Required | Description / values |
|---|---|---|---|
| instrumentId | varchar | Yes | Unique instrument id, e.g. 1594 |
| exchange | varchar | Yes | NSEEQ, NSEFO, BSEEQ, BSEFO, NSECURR, BSECURR, MCXCOMM, NSECOMM, BSECOMM, NCDEXCOMM |
| transactionType | varchar | Yes | BUY, SELL |
| quantity | int | Yes | Units to trade (lots for commodity segments) |
| orderComplexity | varchar | Yes | REGULAR, AMO, BO, CO |
| product | varchar | Yes | NORMAL, INTRADAY, DELIVERY, BNPL |
| orderType | varchar | Yes | LIMIT, MARKET, SL, SLM |
| price | double | Conditional | Required if orderType = LIMIT or SL |
| slTriggerPrice | double | Conditional | Required if orderType = SL or SLM |
| slLegPrice | double | Conditional | Required if orderComplexity = BO or CO |
| targetLegPrice | double | Conditional | Required if orderComplexity = BO |
| validity | varchar | Yes | DAY, IOC |
| disclosedQuantity | int | Optional | Partial quantity disclosed to market |
| marketProtectionPercent | double | Optional | Market protection: a sell is not executed below (or a buy above) the protected price |
| apiOrderSource | varchar | Optional | Source of the API order; fintech partners put their platform name |
| algoId | varchar | Optional | Identifier of the algorithm placing the order |
| orderTag | varchar | Optional | Custom label/tag for the order |

Response:
```json
{
  "status": "Ok",
  "message": "Success",
  "result": [
    {
      "status": "Success",
      "message": "Success",
      "brokerOrderId": "240919000000041",
      "requestTime": "19-Sep-2024 18:48:46"
    }
  ]
}
```
Response fields: `status` (Success or an error code such as EC004), `message` (e.g. "Success", "User blocked"), `brokerOrderId` (IIFL's unique order number), `requestTime` (e.g. 19-Sep-2024 18:48:46).

### 1.2 Modify Order: PUT /orders/{brokerOrderId}

Request (JSON array). Send only the fields to change:
```json
[
  {
    "quantity": "1",
    "orderType": "MARKET",
    "validity": "DAY",
    "price": "1501.55",
    "slTriggerPrice": "1505.2",
    "disclosedQuantity": "500",
    "marketProtectionPercent": "1400"
  }
]
```
| Parameter | Type | Required | Values |
|---|---|---|---|
| quantity | int | Optional | e.g. 100 |
| orderType | varchar | Optional | LIMIT, MARKET, SL, SLM |
| price | double | Conditional | Required if orderType = LIMIT, SL |
| slTriggerPrice | double | Conditional | Required if orderType = SL, SLM |
| validity | varchar | Optional | DAY, IOC |
| disclosedQuantity | int | Optional | |
| marketProtectionPercent | double | Optional | |

Use cases:
- Change quantity to 15: `{ "quantity": "15" }`
- MARKET to LIMIT at 100: `{ "orderType": "LIMIT", "price": "100" }`
- LIMIT to SL with trigger 99, same price: `{ "orderType": "SL", "slTriggerPrice": "99" }`

Response (note: `result` is an object here):
```json
{
  "status": "Ok",
  "message": "Success",
  "result": {
    "status": "Success",
    "message": "Success",
    "brokerOrderId": "240919000000041",
    "requestTime": "19-Sep-2024 18:48:46"
  }
}
```
Limit: **25 modifications per order**.

### 1.3 Cancel Order: DELETE /orders/{brokerOrderId}

No request body. Response has the same shape as Modify (result is an object with status, message, brokerOrderId, requestTime).

### 1.4 Order Book: GET /orders
No body. Response is a JSON array:
```json
[
  {
    "clientId": "TEST102",
    "placedBy": "TEST102",
    "brokerOrderId": "240920000000124",
    "exchangeOrderId": "1100000000089930",
    "orderStatus": "cancelled",
    "formattedInstrumentName": "INFOSYS LIMITED",
    "tradingSymbol": "INFY-EQ",
    "instrumentId": "1594",
    "exchange": "NSEEQ",
    "transactionType": "BUY",
    "quantity": "1",
    "product": "INTRADAY",
    "orderComplexity": "REGULAR",
    "orderType": "LIMIT",
    "price": "180000.0",
    "averageTradedPrice": "0.0",
    "slTriggerPrice": "0.0",
    "validity": "DAY",
    "disclosedQty": "0",
    "marketProtectionPercent": "0.0",
    "exchangeTimestamp": "20-Sep-2024 16:05:33",
    "exchangeUpdateTime": "20-Sep-2024 16:05:32",
    "rejectionReason": "",
    "mainLegOrderId": "NA",
    "pendingQuantity": "1",
    "filledQuantity": "0",
    "appKey": "NA",
    "apiOrderSource": "API",
    "algoId": "",
    "source": "API",
    "orderTag": "NA",
    "brokerUpdateTime": "17-Feb-2025 06:48:39"
  }
]
```
Field notes: `orderStatus` examples: Open, Complete, Rejected, Cancelled (lowercase in samples, e.g. "rejected", "cancelled"). `rejectionReason` example: `RMS:Margin Exceeds,****duct`. `mainLegOrderId` is the parent order id for BO/CO legs. `brokerUpdateTime` format `DD-Mon-YYYY HH:MM:SS`.

### 1.5 Order History: GET /orders/{brokerOrderId}
No body. JSON array (history entries). Same fields as the order book, with `disclosedQuantity` instead of `disclosedQty`, plus `cancelledQuantity`. Source sample shows a cancelled order with `cancelledQuantity: "1"` and `pendingQuantity: "1"`.

### 1.6 Order History Lite: GET /orders/lite/{brokerOrderId}
No body. Same shape as Order History, returns the latest history object (in a JSON array).

### 1.7 Trade Book: GET /trades
No body. Response is a JSON array:
```json
[
  {
    "clientId": "31625881",
    "placedBy": "31625881",
    "brokerOrderId": "240807000000068",
    "exchangeOrderId": "900000000000000",
    "exchangeTradeId": "893487609000000",
    "formattedInstrumentName": "IDEA VODAFONE",
    "tradingSymbol": "IDEA-EQ",
    "instrumentId": "14366",
    "exchange": "NSEEQ",
    "transactionType": "BUY",
    "product": "NORMAL",
    "orderComplexity": "REGULAR",
    "orderType": "MARKET",
    "validity": "DAY",
    "tradedPrice": "1560",
    "filledQuantity": "50",
    "fillTimestamp": "07-Aug-2024 16:14:17",
    "algoId": "algo123",
    "orderTag": "123abc"
  }
]
```

### 1.8 Order and Trade Updates (Bridge stream)
- Connect with the Bridge package: host `bridge.iiflcapital.com`, port `8883`, token = `<userSession>`.
- Subscribe with `{"subscriptionList": ["<CLIENT_ID>"]}` for order updates and trade updates.
- Callbacks: `on_order_updates_received`, `on_trade_updates_received` (JSON payloads).
- Connection ack example: `{"packetType": 2, "packetName": "CONNACK", "status": 0, "message": "Success"}`.

Order update packet fields: clientId, validity, orderComplexity, product, orderType, tradingSymbol, transactionType, instrumentId, price, slTriggerPrice, quantity, disclosedQuantity, cancelledQuantity, algoId, marketProtectionPercent, placedBy, averageTradedPrice, filledQuantity, pendingQuantity, brokerOrderId, exchangeOrderId, rejectionReason, orderStatus, exchangeTimestamp, exchangeUpdateTime, mainLegOrderId, validityDate, source, comments, brokerUpdateTime.

Trade update packet fields: tradedPrice, filledQuantity, exchangeTradeId, instrumentId, exchange, clientId, orderComplexity, product, tradingSymbol, fillDate, fillTime, brokerOrderId, exchangeOrderId, transactionType, orderType, placedBy, algoId. (Sample shows fillDate "45511" and fillTime "0.676585648148148", spreadsheet-style serial values; the table says 8/7/2024 and 16:14:17.)

---

## 2. Portfolio

| Method | Endpoint | Action |
|---|---|---|
| GET | /holdings | Long-term equity holdings (settled T+1) |
| GET | /positions | All open positions for the day, incl. F&O carry-forward |
| PUT | /positions | Position conversion (Intraday <-> Delivery) |

### 2.1 Holdings: GET /holdings (no body)
Sample object:
```json
{
  "isin": "INE002A01018",
  "nseInstrumentId": "2885",
  "bseInstrumentId": "500325",
  "nseTradingSymbol": "RELIANCE-EQ",
  "bseTradingSymbol": "RELIANCE",
  "formattedInstrumentName": "RELIANCE INDUSTRIES LTD.",
  "product": "DELIVERY",
  "totalQuantity": 300,
  "dpQuantity": 300,
  "collateralQuantity": 0,
  "t1Quantity": 0,
  "authorizedQuantity": "0",
  "averageTradedPrice": "280000.00",
  "previousDayClose": null
}
```
Notes: product is DELIVERY or BNPL. `totalQuantity = dpQuantity + t1Quantity`. `t1Quantity` is quantity pending settlement from yesterday's trades. `collateralQuantity` is pledged.

### 2.2 Positions: GET /positions (no body)
Sample object:
```json
{
  "instrumentId": "35382",
  "tradingSymbol": "NIFTY24OCTFUT",
  "formattedInstrumentName": "NIFTY 31 Oct 2024",
  "exchange": "NSEFO",
  "product": "NORMAL",
  "netQuantity": 26,
  "netAveragePrice": "0",
  "overnightQuantity": 1,
  "overnightPrice": "0",
  "buyQuantity": 25,
  "buyPrice": 652000,
  "sellQuantity": 0,
  "sellPrice": 0,
  "dayBuyQuantity": "25",
  "dayBuyPrice": 652000,
  "dayBuyValue": "627961.54",
  "daySellQuantity": "0",
  "daySellPrice": "0.00",
  "daySellValue": "0.00",
  "multiplier": "1",
  "lotSize": "25",
  "tickSize": "0.05",
  "previousDayClose": "26308.85"
}
```
Also documented: `realizedPnl` (double, profit/loss booked from closed trades).
Formulas from the source:
- netQuantity = overnightQuantity + dayBuyQuantity - daySellQuantity
- netAveragePrice = (overnightQuantity*overnightPrice + dayBuyValue - daySellValue) / netQuantity
- buyQuantity = max(overnightQuantity, 0) + dayBuyQuantity
- buyPrice = (overnightPrice * max(overnightQuantity,0) + dayBuyPrice * dayBuyQuantity) / buyQuantity
- sellQuantity = max(-1*overnightQuantity, 0) + daySellQuantity
- sellPrice = (overnightPrice * max(-1*overnightQuantity,0) + daySellPrice * daySellQuantity) / sellQuantity
- dayBuyValue = dayBuyQuantity * dayBuyPrice; daySellValue = daySellQuantity * daySellPrice
- `multiplier`: quantity/lot multiplier used for P&L

### 2.3 Position Conversion: PUT /positions
Request:
```json
{
  "instrumentId": "532540",
  "exchange": "BSEEQ",
  "transactionType": "BUY",
  "conversionQuantity": "1",
  "positionType": "day",
  "oldProduct": "INTRADAY",
  "newProduct": "Delivery"
}
```
All fields required: instrumentId, exchange (NSEEQ, NSEFO...), transactionType (BUY/SELL), conversionQuantity (int), positionType (description in the source is garbled: "DAY, IOC"), oldProduct (INTRADAY), newProduct (DELIVERY).
Response:
```json
{ "status": "Ok", "message": "Success", "result": { "status": "Success", "message": "Success" } }
```
`result[].status` may be Success or an error such as EC003.

---

## 3. Market Data APIs (REST)

| Method | Endpoint | Action |
|---|---|---|
| POST | /marketdata/historicaldata | Historical candles (OHLCV) |
| POST | /marketdata/marketquotes | Real-time quotes, single or bulk |
| POST | /marketdata/marketdepth | Market depth |
| POST | /marketdata/openinterest | Open interest for F&O |

### 3.1 Historical data: POST /marketdata/historicaldata
Request:
```json
{
  "exchange": "NSEEQ",
  "instrumentId": "1594",
  "interval": "1 minute",
  "fromDate": "19-Sep-2024",
  "toDate": "20-Sep-2024"
}
```
- exchange: NSEEQ, NSEFO, BSEEQ, BSEFO, NSECURR, BSECURR, MCXCOMM, NCDEXCOMM, NSECOMM, BSECOMM
- interval: `1 minute`, `5 minutes`, `10 minutes`, `15 minutes`, `30 minutes`, `60 minutes`, `1 day`, `weekly`, `monthly`
- dates in `DD-MMM-YYYY` (e.g. 19-Sep-2024)

The source states: the response is returned **in string format, not JSON**. Sample:
```json
{
  "status": "Ok",
  "message": "Success",
  "result": [
    {
      "candles": [
        ["2024-11-11T09:15:00", 68.37, 68.7, 68.15, 68.18, 1494],
        ["2024-11-11T09:16:00", 295.0, 296.3, 293.75, 295.3, 11315876]
      ]
    }
  ]
}
```
Candle array order: initial timestamp, open, high, low, close, volume.

### 3.2 Market quotes: POST /marketdata/marketquotes
Request (array):
```json
[
  { "exchange": "NSEEQ", "instrumentId": "1594" },
  { "exchange": "NSEEQ", "instrumentId": "2885" }
]
```
Response:
```json
{
  "status": "Ok",
  "message": "Success",
  "result": [
    {
      "exchange": "NSEEQ",
      "instrumentId": 1594,
      "ltp": 1412.95,
      "lastTradedQuantity": 5,
      "averageTradedPrice": 1412.47,
      "tradedVolume": 7360198,
      "open": 1396,
      "high": 1421.75,
      "low": 1395.55,
      "close": 1389.65,
      "bestBidPrice": 2994.25,
      "bestBidQuantity": 4,
      "bestAskPrice": 2994.75,
      "bestAskQuantity": 13,
      "totalBidQuantity": 404715,
      "totalAskQuantity": 216809,
      "tickTimestamp": "19-Sep-2024 09:15:00"
    }
  ]
}
```
`close` is the previous session's close. `tickTimestamp` is the timestamp of the latest price update. (In the source, the first sample object spells one key `besAskPrice`.)

### 3.3 Market depth: POST /marketdata/marketdepth
Request: `{ "exchange": "NSEEQ", "instrumentId": "1594" }`
Response:
```json
{
  "status": "Ok",
  "message": "Success",
  "result": {
    "exchange": "NSEFO",
    "instrumentId": 408065,
    "totalBidQuantity": 404715,
    "totalAskQuantity": 216809,
    "marketDepth": {
      "bids": [ { "price": 110, "quantity": 10, "orders": 30 } ],
      "asks": [ { "price": 14311, "quantity": 100, "orders": 4 } ]
    }
  }
}
```
(Sample has 5 levels each for bids and asks.)

### 3.4 Open interest: POST /marketdata/openinterest
Request: `{ "exchange": "NSEEQ", "instrumentId": "1594" }`
Response (no status/result wrapper in the sample):
```json
{
  "exchange": "NSEFO",
  "instrumentId": 408065,
  "openInterest": 7239000,
  "dayHighOi": 87863000,
  "dayLowOi": 69892000
}
```

---

## 4. Market Data Stream (Bridge package, binary)

- Max 6000 event subscriptions per client; max 1024 subscription requests at once.
- Connect: host `bridge.iiflcapital.com`, port `8883`, token `<userSession>`.
- Subscribe: `{"subscriptionList": ["nseeq/2885","nsefo/35005"]}`. Event names are **case-sensitive, lowercase**.
- Topic template for feed, OI, LPP: `<exchange>/<instrumentId>` (e.g. `nseeq/2885`, `nsefo/35217`, `bseeq/999901` for SENSEX). For market status, circuits and 52-week events: `<exchange>` (e.g. `nseeq`).
- Binary values are **little-endian**. Divide prices by `priceDivisor`. Timestamps are Unix epoch.

| Event | Callback | Bytes | Description |
|---|---|---|---|
| Market Feed | on_feed_data_received | 188 | Prices, volumes, depth for equities, F&O, indices |
| Open Interest | on_open_interest_data_received | 16 | Current, day high and day low OI |
| Market Status | on_market_status_data_received | 2 | Open and close events of normal market sessions |
| Upper Circuit | on_upper_circuit_data_received | 12 | Security hits upper DPR |
| Lower Circuit | on_lower_circuit_data_received | 12 | Security hits lower DPR |
| LPP | on_lpp_data_received | 12 | Limit order price range for F&O |
| 52 Week High | on_high_52_week_data_received | 12 | New 52-week high |
| 52 Week Low | on_low_52_week_data_received | 12 | New 52-week low |

### 4.1 Market Feed packet (188 bytes)
| Field | Type | Bytes |
|---|---|---|
| ltp | Int32 | 0-3 |
| lastTradedQuantity | UInt32 | 4-7 |
| tradedVolume | UInt32 | 8-11 |
| high | Int32 | 12-15 |
| low | Int32 | 16-19 |
| open | Int32 | 20-23 |
| close (previous session) | Int32 | 24-27 |
| averageTradedPrice | Int32 | 28-31 |
| reserved | UInt16 | 32-33 |
| bestBidQuantity | UInt32 | 34-37 |
| bestBidPrice | Int32 | 38-41 |
| bestAskQuantity | UInt32 | 42-45 |
| bestAskPrice | Int32 | 46-49 |
| totalBidQuantity | UInt32 | 50-53 |
| totalAskQuantity | UInt32 | 54-57 |
| priceDivisor | Int32 | 58-61 |
| lastTradedTime | Int32 | 62-65 |
| marketDepth | | 66-185 |
| ignored | Int16 | 186-187 |

marketDepth layout: 5 bid levels at bytes 66-125, then 5 ask levels at bytes 126-185. Each level is 12 bytes: quantity (UInt32), price (Int32), orders (Int16), ignored (Int16). For indices, depth and volume entries are 0.

### 4.2 Open Interest packet (16 bytes)
openInterest Int32 (0-3), dayHighOi Int32 (4-7), dayLowOi Int32 (8-11), previousOi Int32 (12-15).

### 4.3 Market Status packet (2 bytes)
MarketStatusCode Int16 (0-1):

| Code | Status |
|---|---|
| 0 | Pre-Open Started |
| 1 | Pre-Open Closed |
| 2 | Market opened |
| 3 | Call Auction Started |
| 4 | Call Auction Closed |
| 5 | Auction Market Started |
| 6 | Auction Market Closed |
| 7 | Market Closed |
| 8 | Closing Session has opened |
| 9 | Closing Session has Closed |
| 10 | Halt |

### 4.4 Circuit, LPP, 52-week packets (12 bytes each)
- Upper circuit: instrumentId UInt32 (0-3), upperCircuit UInt32 (4-7), priceDivisor Int32 (8-11)
- Lower circuit: instrumentId UInt32 (0-3), lowerCircuit UInt32 (4-7), priceDivisor Int32 (8-11)
- LPP: lppHigh UInt32 (0-3), lppLow UInt32 (4-7), priceDivisor Int32 (8-11)
- 52 week high: instrumentId UInt32 (0-3), 52WeekHigh UInt32 (4-7), priceDivisor Int32 (8-11)
- 52 week low: instrumentId UInt32 (0-3), 52WeekLow UInt32 (4-7), priceDivisor Int32 (8-11)

### 4.5 Bridge SDK status codes
| Status code | Message |
|---|---|
| 101 | Request cannot be null |
| 102 | TopicList cannot be null and number of topics must be less than 1024 |
| 103 | Subscription failed |
| 105 | Client is already connected |
| -1 | Generic error |
| 1 | Token is invalid |
| 0 | Success, no error |
| 4 | The client is not currently connected |

Result codes: 0 = Topic granted, 104 = Invalid topic, 128 = Topic not granted.
Acknowledgement packet types: 2 = Connection ack, 9 = Subscription ack, 11 = Unsubscription ack, 14 = Disconnect ack.

---

## 5. Brokerage and Charges APIs

These use a **different base URL**: `https://brkschemeapp.azurewebsites.net/api/brokerageScheme`
Header: `Authorization: Bearer <userSession>` (as stated in the source; confirm with IIFL before sending a session token to this host).

| Method | Endpoint | Action |
|---|---|---|
| POST | https://brkschemeapp.azurewebsites.net/api/brokerageScheme/getCashBrokerageCharges | Cash segment charges |
| POST | https://brkschemeapp.azurewebsites.net/api/brokerageScheme/getFnoCharges | F&O charges |
| POST | https://brkschemeapp-points.azurewebsites.net/api/brokerageScheme/getCommodityCharges | Commodity charges |

Request:
```json
{
  "clientCode": "31775624",
  "scripDetails": { "scripCode": 532648, "exchange": "b", "exchangeType": "c" },
  "orderDetails": { "orderPrice": 83.45, "product": "d", "qty": 5, "multiplier": 1, "orderType": "b" }
}
```
Letter codes: exchange `n` NSE, `b` BSE, `m` MCX. exchangeType `c` Cash, `d` Derivatives (F&O of NSE, BSE and MCX), `y` NSE and BSE commodity. product `d` delivery, `t` intraday. orderType `b` buy, `s` sell.

Response:
```json
{
  "succeeded": true,
  "message": [],
  "errors": [],
  "data": {
    "clientCode": "31798624",
    "orderDetails": { "orderPrice": 83.45, "netQty": 5, "turnOver": 417.25 },
    "scripDetails": { "scripCode": 532648, "exchange": "b", "exchangeType": "c" },
    "transactionCharges": {
      "brokerage": 0.04,
      "charges": {
        "exchangeCharges": { "key": "0.00375% of Turnover", "value": 0.02 },
        "stt": { "key": "0.1% of Turnover", "value": 0.42 },
        "sebiCharges": { "key": "0.0001% of Turnover", "value": 0 },
        "stampDuty": { "key": "0.015% of Turnover", "value": 0.06 },
        "dpCharges": { "key": "Zero Charges", "value": 0 },
        "clearingCharges": { "key": "Zero Charges", "value": 0 },
        "investoryProtectionfund": { "key": "Zero Charges", "value": 0 },
        "gst": { "key": "18% on (Brokerage + Exchange Charges + SEBI Fees + Clearing Charges)", "value": 0.01 }
      },
      "totalCharges": 0.55
    }
  }
}
```
turnOver = orderPrice x netQty.

---

## 6. Annexure

### 6.1 Instrument master (contract files)
CSV: `https://api.iiflcapital.com/v1/contractfiles/<SEGMENT>.csv`
JSON: `https://api.iiflcapital.com/v1/contractfiles/<SEGMENT>.json`
Segments: NSEEQ, NSEFO, NSECOMM, MCXCOMM, INDICES, NSECURR, BSEEQ, BSEFO, BSECURR.
The JSON links can also be called with GET as APIs. **No authorization is required.**

### 6.2 Rate limits (requests per second, per session)
| API | Non-registered (<10 OPS) | Registered (>10 OPS) |
|---|---|---|
| User: Logout | 2 | 2 |
| User: Get User Session | 3 | 3 |
| User: Profile | 3 | 3 |
| User: Limits | 10 | 20 |
| Margin: Pre order margin | 10 | 20 |
| Margin: SPAN exposure | 10 | 20 |
| Place Order | 10 (combined limit for place/modify/cancel, per the source note) | 20 |
| Modify Order | (within combined limit) | 20 |
| Cancel Order | (within combined limit) | 20 |
| Cancel All Orders | 3 | 3 |
| Get Order Book | 3 | 3 |
| Get Order History | 10 | 10 |
| Trade Book | 3 | 3 |
| Positions | 3 | 3 |
| Holdings | 3 | 3 |
| Historical Data | 10 | 10 |
| Market Depth | 10 | 10 |
| Open Interest | 10 | 20 |
| Market Quotes | 10 | 10 |
| Contract Master (each segment) | 2 | 2 |

### 6.3 Trading API error codes
Order rejection reasons are not API errors and are not in this list.

```
EC003 Something Went Wrong, Please Try After Sometime.
EC900 Invalid parameter : 'exchange' cannot be empty or null.
EC901 Invalid parameter : 'exchange' Accepts only {'NSEEQ','NSEFO','BSEEQ','BSEFO','NSECURR','BSECURR','MCXCOMM','NCDEXCOMM','NSECOMM','BSECOMM'}
EC902 Invalid parameter : 'tradingSymbol' cannot be empty or null.
EC903 Invalid parameter : 'quantity' cannot be empty or null.
EC904 Invalid parameter : 'quantity' must be a positive integer.
EC905 Invalid parameter : 'retention' cannot be empty or null.
EC906 Invalid parameter : 'product' cannot be empty or null.
EC907 Invalid parameter : 'transactionType' cannot be empty or null.
EC908 Invalid parameter : 'token' cannot be empty or null.
EC909 Invalid parameter : 'disclosedQty' cannot be empty or null.
EC910 Invalid parameter : 'price' cannot be empty or null.
EC911 Invalid parameter : 'triggerPrice' cannot be empty or null.
EC912 Error : Error while placing order.
EC913 Error : Error while fetching user details.
EC914 Invalid parameter : 'Request parameter' cannot be empty or null.
EC915 Error : Error while fetching order book.
EC916 Error : No order's found for this user.
EC917 Error : Error while fetching order history.
EC918 Error : No order history found for given order id.
EC919 Error : Error while fetching position book.
EC920 Error : No Positions found for this user.
EC921 Error : Error while fetching holdings.
EC922 Error : No holdings found for this user.
EC923 Error : Error while fetching profile details.
EC924 Error : Error while fetching RMS limits.
EC925 Invalid parameter : 'nestOrderNo' cannot be empty or null.
EC926 Error : No Trade's are found for this user.
EC927 Error : Error while fetching trade book.
EC928 Invalid parameter : 'product' Accepts only {'NORMAL','INTRADAY','DELIVERY','BNPL'}.
EC929 Invalid parameter : 'transactionType' Accepts only { 'BUY', 'SELL' }.
EC930 Invalid parameter : 'orderType' Accepts only { 'LIMIT', 'MARKET', 'SL', 'SLM'}.
EC931 Invalid parameter : 'orderType' Accepts only { 'REGULAR', 'BO', 'CO', 'MTF', 'AMO', 'BRACKETORDER', 'COVERORDER','MUTUALFUND', 'AFTERMARKETORDER' }.
EC932 Invalid parameter : 'validity' Accepts only { 'DAY', 'IOC' }.
EC933 Invalid parameter : 'priceType' cannot be empty or null.
EC934 Invalid parameter : 'orderType' cannot be empty or null.
EC935 Error : Error while fetching the single order margin.
EC936 Invalid parameter : 'product' cannot be empty or null.
EC937 Error : Error while canceling all order's.
EC938 Error : Error No Open order's to cancel the order's from orderbook.
EC939 Error : Error while fetching the span margin.
EC940 Error : Error while logging out.
EC941 Invalid parameter : 'instrumentId' cannot be empty or null.
EC942 Invalid parameter : 'orderComplexity' cannot be empty or null.
EC943 Invalid parameter : 'orderComplexity' Accepts only { 'REGULAR', 'AMO', 'BO' , 'CO' }.
EC944 Invalid parameter : 'validity' cannot be empty or null.
EC945 Invalid parameter : 'brokerOrderId' cannot be empty or null.
EC946 Error : Invalid : 'instrumentId' must contain only numeric characters.
EC947 Error : 'instrumentId' does not exist.
EC948 Error : 'quantity' should not be more than 50000000.
EC949 Error : 'quantity' must be a positive integer.
EC950 Invalid parameter: 'price' is required and cannot be empty or null.
EC951 Invalid parameter: 'slTriggerPrice' is required and cannot be empty or null.
EC952 Invalid parameter: 'slLegPrice' cannot be null or empty for BO/CO order .
EC953 Invalid parameter: 'targetLegPrice' is required and cannot be empty or null.
EC954 Error : 'quantity' Should be multiple of lotsize.
EC955 Error : 'product' only Intraday is allowed for BO/CO orders.
EC956 Error : Orders for DELIVERY and BNPL are only allowed on NSEEQ and BSEEQ exchanges.
EC957 Error : Invalid 'price'.
EC958 Error : 'price' cannot be zero or negative.
EC959 Error : Invalid 'slTriggerPrice'.
EC960 Error : 'slTriggerPrice' cannot be zero or negative.
EC961 Error : Invalid 'slLegPrice'.
EC962 Error : 'slLegPrice' cannot be zero or negative.
EC963 Error : Invalid 'targetLegPrice'.
EC964 Error : 'targetLegPrice' cannot be zero or negative.
EC965 Error : For BO orders, only LIMIT or SL 'orderType' is allowed.
EC966 Error : 'trailingSlAmount' cannot be empty or null for OrderType SL.
EC967 Error : 'trailingSlAmount' must be a positive number.
EC968 Error : 'trailingSlAmount' cannot be zero or negative.
EC969 Error : 'Product' only 'NORMAL' and 'INTRADAY' are allowed
EC970 Error : 'disclosedQuantity' allowed only for 'NSEEQ','BSEEQ','MCXCOMM','NSECURR','BSECURR'
EC970 Error : As compared to front end 'disclosedQuantity' should not be consider for F&O
EC971 Error : 'orderTag' characters should be less than or equal to 50 characters
EC972 Error : 'algoId' characters should be less than or equal to 12 characters
EC973 Error : Buy order 'slTriggerPrice' should be less than 'price'
EC974 Error : Sell order 'slTriggerPrice' should be greater than 'price'
EC975 Error : 'disclosedQuantity' cannot exceed the total order 'quantity'.
EC976 Error : 'slLegPrice' cannot be zero or null.
EC977 Error : 'slTriggerPrice' should be used only in 'SL' or 'SLM' OrderType
EC978 Error : 'price' should be used only in 'LIMIT' or 'SL' OrderType
EC979 Invalid 'brokerOrderId'.
EC980 Invalid 'instrumentId'.
EC981 Error : Invalid 'disclosedQty'.
EC982 Error : For 'AMO' 'disclosedQuantity' should be zero.
EC983 Invalid 'algoId'
EC984 Invalid 'orderTag'
EC985 Invalid 'brokerOrderId'.
EC986 Invalid parameter : SpanMargin not allowed for 'NSEEQ' and 'BSEEQ'
EC987 Invalid parameter : 'exchange' Accepts only {'NSEFO','BSEFO','NSECURR','BSECURR','MCXCOMM','NCDEXCOMM','NSECOMM','BSECOMM'}
EC988 Error : 'marketProtectionPercent ' must be a positive number.
EC989 Error : For CO orders, only LIMIT or MARKET 'orderType' is allowed.
EC990 Error : 'quantity' Should be multiple of lotsize.
EC991 Error : 'disclosedQuantity' Should be multiple of lotsize.
EC992 Error : Not able modify the given order. 'brokerOrderId' is
EC993 Error : Provided 'brokerOrderId' not in valid state to modify the order. 'brokerOrderId' is
EC994 Error : Given 'brokerOrderId' is not in your ordebook.
EC995 Error : There is no order to cancel.
EC996 Error : 'validity' IOC is not allowed for AMO order.
EC997 Error : The specified order is not available in the order book and cannot be cancelled. Please verify the order details and try again.
EC998 Error : The specified order is not available in the order book, and order history cannot be retrieved. Please verify the order ID and try again.
EC999 Error : The specified order is not available in the order book and cannot be modified. Please verify the order details and try again.
EC701 Error : Orders with Exchange 'BSEEQ/BSEFO/BSECURR', Cannot be modified to Order Type 'SL' (Stop Loss).
EC702 Error : Only price field can be modified in Sub leg Order's.
EC703 Error : Your account is in a dormant state and trading is not permitted. Please contact support to reactivate your account.
EC704 (same message as EC703)
EC705 (same message as EC703)
EC900 Invalid parameter: 'instrumentId' cannot be empty or null.   (second use of EC900 in the market data section)
EC801 Invalid parameter: 'interval' cannot be empty or null.
EC802 Invalid parameter: 'interval' Accepts only {"1 minute", "5 minutes", "10 minutes", "15 minutes", "30 minutes", "60 minutes", "1 day"}
EC803 Invalid parameter: 'fromDate' cannot be empty or null.
EC804 Invalid fromDate format. Please enter the fromDate in DD-MMM-YYYY format (e.g., 20-Sep-2020).
EC805 Invalid parameter: 'toDate' cannot be empty or null.
EC806 Invalid toDate format. Please enter the toDate in DD-MMM-YYYY format (e.g., 20-Sep-2020).
EC807 Invalid date range. The 'fromDate' cannot be greater than the 'toDate'.
EC808 Error : Invalid : 'instrumentId' must be greater than 0.
EC809 Maximum permissible date range limit reached
EC810 From date has to be less than end date
EC001 Invalid parameter : 'UserIdentity' cannot be empty or null.
EC002 Client does not exist.
EC004 User blocked.
EC005 Invalid parameter : Request is null or empty.
EC006 Invalid parameter : 'IsPANEntered' cannot be empty or null.
EC007 Invalid parameter : 'IsPANEntered', IsPANEntered will consists only Y and N.
EC008 Invalid parameter : 'UserId' cannot be empty or null.
EC009 Invalid parameter : 'Password' cannot be empty or null.
EC010 Invalid parameter : 'appName' cannot be empty or null.
EC011 Multiple client IDs associated with the provided details. Please specify a client code to proceed further.
EC012 (same message as EC011)
EC013 (same message as EC011)
EC014 Your account has been blocked by administrator. Please contact admin (Unblock account option wont work).
EC015 Not able to send OTP for the registered mobile and email.
EC016 Invalid userId or password.
EC017 Invalid parameter : 'osName' cannot be empty or null.
EC018 Your OTP has been initiated. Please try again after 60 seconds.
EC019 You have exceeded the maximum limit for OTP requests. Please try again later.
EC020 We are currently unable to send OTP. Please contact the administrator for assistance.
EC021 Invalid parameter : 'ClientCode' cannot be empty or null.
EC022 Invalid parameter : 'ReceivedOTP' cannot be empty or null.
EC023 Invalid parameter : 'IsPANEntered' cannot be empty or null.
EC024 Your OTP is expired.
EC025 Invalid OTP.
EC026 Invalid parameter : 'userId' cannot be empty or null.
EC027 Invalid parameter : 'pan' cannot be empty or null.
EC028 Invalid parameter : 'osName' cannot be empty or null.
EC029 Invalid parameter : 'dob' cannot be empty or null.
EC030 Invalid user.
EC031 The PAN provided is invalid. Please check and enter a valid PAN.
EC032 (same message as EC031)
EC033 Invalid parameter : 'otp' cannot be empty or null.
EC034 Invalid parameter : 'totp' cannot be empty or null.
EC035 The T-OTP is already enabled for this account.
EC036 Invalid T-OTP.
EC037 Only MOB keys are accepted for this operation.
EC038 Invalid parameter : 'token' cannot be empty or null.
EC039 Invalid parameter : 'deviceId' cannot be empty or null.
EC040 Invalid parameter : 'deviceType' cannot be empty or null.
EC041 Invalid parameter : 'enable' cannot be empty or null.
EC042 Error.
EC043 T-OTP Not enabled for this account.
EC044 Error : Token is not valid.
EC045 Token is valid.
EC046 Invalid parameter : 'osName' cannot be empty or null.
EC047 Invalid parameter : 'versionNo' cannot be empty or null.
EC048 Invalid parameter : 'deviceId' cannot be empty or null.
EC049 Invalid parameter : 'deviceName' cannot be empty or null.
EC050 Invalid parameter : 'macAddress' cannot be empty or null.
EC051 Error : Only mobile OS are allowed to call this method.
EC052 Error : You have reached the MAX OTP limit, Kindly try again after 15 minutes.
EC053 Invalid parameter : 'appKey' cannot be empty or null.
EC054 Error : Given appkey is InCorrect, Please enter the valid appkey.
EC055 Error : Unauthorized access. Please authorize the application to obtain the auth code.
EC056 Invalid parameter : 'ceData' cannot be empty or null.
EC057 Invalid parameter : 'ceEncData' cannot be empty or null.
EC058 Error : Invalid 'deviceId'.
EC059 Error : Invalid 'versionNo'.
EC060 Error : Invalid 'appName'.
EC061 Error : Invalid 'osName'.
EC062 Error : Illegal or Expired Encryption key.
EC063 Error : 'password' should not contain spaces.
EC064 Multiple client IDs associated with the provided email. Please specify a client code to proceed further.
EC065 Multiple client IDs associated with the provided mobile. Please specify a client code to proceed further.
EC066 Multiple client IDs associated with the provided details. Please specify a client code to proceed further.
EC067 Error : Provide pan is not mapped with any client Id.
EC068 Error : Provide userId is not mapped with given pan.
EC069 Error : Your password has expired. Please change your password to continue.
EC070 Invalid parameter : 'version' cannot be empty or null.
EC071 Invalid parameter : 'os' cannot be empty or null.
EC072 Mobile Number and email ID are invalid, please write to cs@iifl.com to update your Mobile Number & email ID.
EC073 Error : Please enter the date of birth (DOB) in MMDDYYYY format.
EC074 Error : Invalid 'osName'.
EC075 Error : Unauthorized access.
EC076 Error : This service is not available for your account type.
EC077 Error : Provide PAN is not mapped with given user Id.
EC078 Error : Provide PAN is not mapped with given user Id.
EC079 Error : Password must have one allowed special character out of @ # $ % & * / \
EC080 Error : Too many incorrect OTP attempts. Request a new OTP after 15 minutes.
EC081 Error : Your account is in a voluntary freeze state, and login is not permitted. Please contact the administrator for assistance.
```

### 6.4 RMS order rejections
All RMS rejections start with `RMS:`. A rejection message without `RMS:` is not an RMS rejection. Possible rejections (generated dynamically from block details, rules, or order validation level):
- `RMS:Blocked for <block details>`
- `RMS:Rule: <rule name and order validation level>`
- `RMS:Margin Exceeds, Required:<Value>, Available:<Value>` (validation level appended)
- `RMS:MtoM Exceeds, Required:<Value>, Available:<Value>` (validation level appended)
- `RMS:Lt rate not found for rule:` (rule name and validation level appended)
- `RMS:Asset exchange segment not found for rule:`
- `RMS:Field Not Found <MNM_ID>`
- `RMS:Bad Input`
- `RMS:Scrip Not found in Mrv master`
- `RMS:Mrv Master DOWN`
- `RMS:User not enabled on product`
- `RMS:Client not enabled on product`
- `RMS:NO Last Trade Price`
- `RMS:Auto Square Off Block`
- `RMS:Scrip <Symbol> is in Ban Period.`
- `RMS:Index value not found`
- `RMS:Index close value not found`
- `RMS:Entity is not loaded properly`

### 6.5 FAQ highlights
- APIs are free for individual traders, developers and platforms.
- IIFL Capital clients log in to the developer portal with trading account credentials and create an application. Fintech platforms register on the portal and IIFL reviews and approves them.
- Login is required daily; a new unique session token is generated with each login. The token goes in the header of every API request.
- Converting MIS (intraday) to CNC (delivery) or vice versa is **not supported through the API**; it can be done in the IIFL Markets mobile app. (This conflicts with the PUT /positions page in section 2.3.)
- Market data is available through the APIs and the Bridge package. Authorized third-party vendors can also be used. The Bridge package is mandatory for the market data stream.
- Instrument master (all tradable instruments) is available as CSV and JSON (section 6.1).
- Support: openapi@iiflcapital.com, phone/WhatsApp +91-7718830851. Bugs can be raised on the IIFL Capital GitHub issues page.

---

## Notes (not from IIFL): inconsistencies to handle defensively

1. `besAskPrice` typo appears in one marketquotes sample; the table says `bestAskPrice`. Accept both.
2. Quantity is `int` in the parameter tables but a string in the JSON examples. Numbers sent as strings throughout the samples (`"quantity": "1"`).
3. `marketProtectionPercent` example is `"1400"` next to a price of 1501.55, so the unit (percent vs price) is unclear. Leave unset unless confirmed with IIFL.
4. Historical API: `interval` table lists `weekly` and `monthly`, but error EC802 lists only 1 minute to 1 day. The response is described as string format, not JSON. Date range limit exists (EC809).
5. PUT /positions (conversion) conflicts with the FAQ that says API conversion is not supported. Do not use until confirmed.
6. `result` is an array on place order but an object on modify and cancel.
7. Order status strings vary in case and spelling ("Cancelled"/"cancelled"/"Canceled"). A cancelled order sample shows `pendingQuantity: "1"`, so do not rely on pending alone.
8. `orderTag` limit is 50 characters (EC971), not 32.
9. Timestamp formats differ: `DD-Mon-YYYY HH:MM:SS` for orders and quotes, ISO `YYYY-MM-DDTHH:MM:SS` for candles, spreadsheet serial numbers in one stream sample.
10. EC900, EC901 and EC970 each appear twice with different messages.
11. Session expiry may be reported as an error code in a normal response (EC044, EC055, EC075), not only as HTTP 401. Confirm with IIFL.
12. The documentation does not include the login/getUserSession, Profile, Limits, pre-order margin, SPAN margin or Cancel All Orders pages.
