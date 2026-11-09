# SPDX-License-Identifier: GPL-3.0-only
"""The made-up meetings of demo_data.py for the English page: the same projects, meetings and tasks in English.
Values the program stores in Russian (meeting kinds, task states, speaker labels) stay Russian."""

PROJECTS = {
    "Warehouse": {
        "заметки": "1C:Trade Management 11.5, two warehouses: Central and North. On the client side Emma, the chief "
        "accountant; Tom runs the warehouse.",
        "для_модели": True,
        "к_встрече": "Stock by batches\nWarehouse file format for batches",
        "вид": "Показ доработки",
    },
    "Website": {
        "заметки": "Orders are exchanged with the website every 15 minutes. On the client side David, head of sales.",
        "для_модели": True,
        "к_встрече": "Order statuses on the website",
        "вид": "Рабочая встреча",
    },
    "Reports": {
        "заметки": "Management reports: sales, margin, stock. Victor, the CFO, signs them off.",
        "для_модели": False,
        "к_встрече": "",
        "вид": "Согласование ЧТЗ",
    },
}
LOOKS = {
    "Warehouse": {"цвет": 2, "значок": "коробка"},
    "Website": {"цвет": 5, "значок": "дом"},
    "Reports": {"цвет": 4, "значок": "график"},
}
OWNER = "Maria"

MEETINGS = [
    {
        "folder": "2026-10-26_10-00",
        "title": "Stock import sign-off",
        "project": "Warehouse",
        "kind": "Показ доработки",
        "seconds": 2520,
        "names": {"Собеседник 1": "Emma", "Собеседник 2": "Tom"},
        "provider": "OpenRouter, elevenlabs/scribe-v2",
        "speech_cost": 0.31,
        "minutes_cost": 0.12,
        "phrases": [
            (5, "Я", "Good morning. Today we're showing the stock import from the warehouse file."),
            (21, "Собеседник 1", "Great, we're closing the month right now."),
            (70, "Я", "You pick the file, and the program matches the items by SKU."),
            (155, "Собеседник 2", "What if the SKU isn't in the database?"),
            (168, "Я", "Those rows go to a separate tab, you can match them by hand."),
            (252, "Собеседник 1", "Do we take the price from the price list or from the file?"),
            (270, "Я", "From the price list, as we agreed last time."),
            (425, "Собеседник 2", "We need a total for each warehouse, right now there's only the overall one."),
            (460, "Я", "We'll add totals by warehouse by Friday."),
            (738, "Собеседник 1", "Who will check the import on a copy of the database?"),
            (750, "Собеседник 2", "I'll check it on the copy by Wednesday."),
            (1082, "Собеседник 1", "Do we need stock by batches too?"),
            (1100, "Я", "That's a separate change, let's discuss it at the next meeting."),
            (2390, "Я", "So we accept it with the remark on warehouse totals, next meeting on Thursday."),
        ],
        "minutes": """# Minutes of the meeting on 26.10.2026 about the stock import

## What was shown

1. The stock import from the warehouse file, with items matched by SKU. [01:10]
2. A separate tab for rows whose SKU is not in the database. [02:48]

## Remarks

1. A total for each warehouse is needed, now there is only the overall one. Maria, 30.10.2026. [07:05]

## Decisions

1. The import takes the price from the price list. [04:30]
2. The import is accepted with the remark on warehouse totals. [39:50]

## Tasks

1. Check the import on a copy of the database. Tom, 28.10.2026. [12:30]
2. Add a total for each warehouse. Maria, 30.10.2026. [07:40]

## Open questions

1. Is stock by batches needed. We discuss it at the next meeting. [18:02]

## Next meeting

1. 29.10.2026. Stock by batches.

## For the analyst

- Ask Emma for the warehouse file format for batches.
""",
        "notes": """## Повестка

- [x] Stock import
- [x] Prices from the price list
- [ ] Stock by batches

## Заметки

[04:30] Price from the price list - confirmed
[07:05] Totals by warehouse!
""",
        "marks": [425.0],
    },
    {
        "folder": "2026-10-26_14-30",
        "title": "Website exchange stand-up",
        "project": "Website",
        "kind": "Рабочая встреча",
        "seconds": 1500,
        "names": {"Собеседник 1": "David"},
        "provider": "On this computer, GigaAM v3",
        "speech_cost": 0.0,
        "minutes_cost": 0.09,
        "phrases": [
            (8, "Я", "David, about the order exchange: what gets in the way the most right now?"),
            (19, "Собеседник 1", "Orders from the website come in without the payment status."),
            (95, "Я", "We'll add the payment status to the exchange, it's a small change."),
            (240, "Собеседник 1", "And cancelled orders should be released from the reserve."),
            (262, "Я", "Yes, we release the reserve on cancellation."),
            (610, "Собеседник 1", "When can we check it?"),
            (625, "Я", "I'll have it ready by Wednesday, we'll check it together."),
        ],
        "minutes": """# Minutes of the meeting on 26.10.2026 about the website exchange

## Decisions

1. The payment status is sent in the exchange together with the order. [01:35]
2. When an order is cancelled on the website, its reserve is released. [04:22]

## Tasks

1. Add the payment status to the exchange. Maria, 28.10.2026. [01:35]
2. Release the reserve when an order is cancelled. Maria, 28.10.2026. [04:22]
3. Check the exchange on the test website. David, 29.10.2026. [10:25]

## Open questions

1. Which payment statuses the payment service has. David will find out. [00:19]
""",
        "notes": "",
        "marks": [],
    },
    {
        "folder": "2026-10-23_11-00",
        "title": "Discovery: returns to suppliers",
        "project": "Warehouse",
        "kind": "Обследование",
        "seconds": 3300,
        "names": {"Собеседник 1": "Emma", "Собеседник 2": "Tom"},
        "provider": "On this computer, GigaAM v3",
        "speech_cost": 0.0,
        "minutes_cost": 0.14,
        "phrases": [
            (12, "Я", "Tell me how you make a return to a supplier today."),
            (30, "Собеседник 2", "The storekeeper writes the return note by hand, then the accountant enters it."),
            (180, "Собеседник 1", "We often mix up the price: one in the note, another in the receipt."),
            (420, "Я", "So the return should be created from the receipt, with its prices."),
            (436, "Собеседник 1", "Yes, and the note should be printed straight from the database."),
        ],
        "minutes": """# Minutes of the meeting on 23.10.2026 about returns to suppliers

## Current process

1. The storekeeper writes the return note by hand, the accountant then enters it in the database. [00:30]
2. Prices in the return note and in the receipt differ, errors are found at reconciliation. [03:00]

## Requirements

1. A return is created from the receipt, with its prices. [07:00]
2. The return note is printed from the database. [07:16]

## Open questions

1. Who signs the return note for the warehouse. Tom will find out. [08:10]
""",
        "notes": "",
        "marks": [],
    },
    {
        "folder": "2026-10-22_16-00",
        "title": "Spec review: sales report",
        "project": "Reports",
        "kind": "Согласование ЧТЗ",
        "seconds": 2280,
        "names": {"Собеседник 1": "Victor"},
        "provider": "OpenRouter, elevenlabs/scribe-v2",
        "speech_cost": 0.28,
        "minutes_cost": 0.11,
        "phrases": [
            (10, "Я", "Let's go through the sales report spec section by section."),
            (130, "Собеседник 1", "The filters need the sales manager, not only the department."),
            (300, "Собеседник 1", "Margin is calculated from the cost on the date of sale."),
            (900, "Я", "Got it. Do we keep grouping by month?"),
            (915, "Собеседник 1", "Yes, and a total for the year."),
        ],
        "minutes": """# Minutes of the meeting on 22.10.2026 about the sales report spec

## Spec changes

1. Section "Filters" - add a filter by sales manager. [02:10]
2. Section "Measures" - margin is calculated from the cost on the date of sale. [05:00]
3. Section "Grouping" - add a total for the year. [15:15]

## Decisions

1. Grouping by month stays. [15:15]
""",
        "notes": "",
        "marks": [],
    },
    {
        "folder": "2026-10-21_09-30",
        "title": "Project status",
        "project": "Website",
        "kind": "Статус проекта",
        "seconds": 1200,
        "names": {"Собеседник 1": "David"},
        "provider": "On this computer, GigaAM v3",
        "speech_cost": 0.0,
        "minutes_cost": 0.07,
        "phrases": [
            (6, "Я", "A quick status: the product export is ready."),
            (60, "Собеседник 1", "And the prices?"),
            (72, "Я", "Prices are in progress, we'll finish by Friday."),
            (400, "Собеседник 1", "The test website will only be ready in a week."),
        ],
        "minutes": """# Minutes of the meeting on 21.10.2026 about the project status

## Done

1. The product export to the website. [00:06]

## In progress

1. The price export. Maria, 24.10.2026. [01:12]

## Risks

1. The test website will only be ready in a week. Access is needed from David. [06:40]
""",
        "notes": "",
        "marks": [],
    },
    {
        "folder": "2026-10-20_15-00",
        "title": "Storekeeper training",
        "project": "Warehouse",
        "kind": "Обучение",
        "seconds": 2820,
        "names": {"Собеседник 1": "Tom"},
        "provider": "OpenRouter, elevenlabs/scribe-v2",
        "speech_cost": 0.34,
        "minutes_cost": 0.13,
        "phrases": [
            (15, "Я", "I'll show you how to record goods receipt the new way."),
            (600, "Собеседник 1", "What if the supplier brought more than in the order?"),
            (620, "Я", "The surplus goes on a separate line, the program will prompt you."),
            (1500, "Собеседник 1", "It would be handy to print labels right from the receipt."),
        ],
        "minutes": """# Minutes of the meeting on 20.10.2026 about the storekeeper training

## What was shown

1. Goods receipt the new way. [00:15]
2. Recording a surplus on a separate line. [10:20]

## Remarks

1. Labels need to be printed right from the receipt. Maria, 31.10.2026. [25:00]
""",
        "notes": "",
        "marks": [],
    },
]

OWN_TASKS = {
    "свои": [
        {
            "ид": "demo000001",
            "текст": "Write the returns guide",
            "кто": "Maria",
            "срок": "2026-10-31",
            "проект": "Warehouse",
            "состояние": "в работе",
            "создана": "2026-10-23",
            "описание": "Once the returns process is agreed",
            "метки": ["docs"],
            "приоритет": "высокий",
            "чек_лист": [{"текст": "Process", "сделан": True}, {"текст": "Screenshots", "сделан": False}],
        },
        {
            "ид": "demo000002",
            "текст": "Export a test database for the warehouse",
            "кто": "Tom",
            "срок": "2026-10-22",
            "проект": "Warehouse",
            "состояние": "готово",
            "создана": "2026-10-20",
            "описание": "",
            "метки": [],
            "приоритет": "",
            "чек_лист": [],
        },
        {
            "ид": "demo000003",
            "текст": "Send the discovery minutes to the client",
            "кто": "Maria",
            "срок": "2026-10-23",
            "проект": "Warehouse",
            "состояние": "готово",
            "создана": "2026-10-23",
            "описание": "",
            "метки": [],
            "приоритет": "",
            "чек_лист": [],
        },
    ],
    "метки": [{"имя": "docs", "цвет": 5}],
}
