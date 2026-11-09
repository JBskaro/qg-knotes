# SPDX-License-Identifier: GPL-3.0-only
"""Personal data hidden from a cloud model: what becomes a label, what stays (dates, times, sums, task numbers), the
same value keeps its label, the labels go back after the answer."""

import pathlib
import tempfile
import unittest

from knotes import catalog, privacy, transcript, vocabulary


def _hidden(text: str) -> tuple[str, dict[str, str]]:
    hider = privacy.Hider()
    return hider.hide(text), hider.table


class TestWhatIsHidden(unittest.TestCase):
    def test_each_kind_gets_its_label(self):
        for text, label, value in (
            ("Звоните +7 (912) 345-67-89 после обеда", "[ТЕЛЕФОН_1]", "+7 (912) 345-67-89"),
            ("мобильный 8 912 345 67 89, рабочий", "[ТЕЛЕФОН_1]", "8 912 345 67 89"),
            ("номер 89123456789.", "[ТЕЛЕФОН_1]", "89123456789"),
            ("сотовый 912-345-67-89", "[ТЕЛЕФОН_1]", "912-345-67-89"),
            ("городской 345-67-89", "[ТЕЛЕФОН_1]", "345-67-89"),
            ("в Казахстан +7 701 123 4567", "[ТЕЛЕФОН_1]", "+7 701 123 4567"),
            ("Германия +49 30 1234 5678", "[ТЕЛЕФОН_1]", "+49 30 1234 5678"),
            ("+7 912 345 67 89 10 минут", "[ТЕЛЕФОН_1]", "+7 912 345 67 89"),
            ("+44 20 7946 0958 - 15 минут", "[ТЕЛЕФОН_1]", "+44 20 7946 0958"),
            ("пишите на ivan.petrov@romashka.ru, ответит", "[EMAIL_1]", "ivan.petrov@romashka.ru"),
            ("почта анна@почта.рф", "[EMAIL_1]", "анна@почта.рф"),
            ("ИНН организации 7707083893.", "[ИНН_1]", "7707083893"),
            ("ИП, ИНН: 500100732259", "[ИНН_1]", "500100732259"),
            ("реквизиты 7707083893 и всё", "[ИНН_1]", "7707083893"),
            ("СНИЛС 112-233-445 95", "[СНИЛС_1]", "112-233-445 95"),
            ("номер 11223344595 в анкете", "[СНИЛС_1]", "11223344595"),
            ("паспорт серия 45 06 номер 123456", "[ПАСПОРТ_1]", "45 06 номер 123456"),
            ("паспорт 4506 123456 выдан", "[ПАСПОРТ_1]", "4506 123456"),
            ("карта 4111 1111 1111 1111 привязана", "[КАРТА_1]", "4111 1111 1111 1111"),
            ("карта 4111111111111111", "[КАРТА_1]", "4111111111111111"),
            ("р/с 40702810900000001234 в банке", "[СЧЕТ_1]", "40702810900000001234"),
            ("склад на улице Ленина, дом 5, кв. 12 рядом", "[АДРЕС_1]", "улице Ленина, дом 5, кв. 12"),
            ("офис: ул. Тверская д. 7 стр. 2", "[АДРЕС_1]", "ул. Тверская д. 7 стр. 2"),
        ):
            with self.subTest(text=text):
                hidden, table = _hidden(text)
                self.assertIn(label, hidden)
                self.assertEqual(table, {label: value})
                self.assertEqual(text.replace(value, label), hidden)

    def test_numbers_that_stay(self):
        """Dates, times, sums, versions, task and document numbers are not personal data."""
        for text in (
            "[05:40] Я: Встреча 22.10.2026 в 10:18, срок 2026-11-01.",
            "[1:09:10] Собеседник 1: Бюджет 1 500 000 рублей, аванс 150 000, остаток 3,5 млн.",
            "Платформа 8.3.25.1286, конфигурация 3.1.30.115, релиз 2.5.18.92.",
            "Задача 12, вопрос 3, заказ № 345, счет 1024 от 01.10.2026, 15% скидка.",
            "В 2018 году было 8 складов и 900 сотрудников, 950 000 рублей в месяц.",
            "Карта 1234 5678 9012 3456 без проверки Луна, номер 1234567890 без ИНН.",
            "Время 12:30-13:45, перерыв 15 минут, 2 часа 30 минут.",
            "Курс 95,50, артикул 100-200, документ ЗК00-000123, телефон не дали.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_hidden(text), (text, {}))

    def test_same_value_same_label(self):
        hider = privacy.Hider()
        first = hider.hide("Звоните +7 912 345-67-89 или на a@b.ru")
        second = hider.hide("Повторю: 8 (912) 345-67-89, почта A@B.RU, другой 8 999 000 11 22")
        self.assertEqual(first, "Звоните [ТЕЛЕФОН_1] или на [EMAIL_1]")
        self.assertEqual(second, "Повторю: [ТЕЛЕФОН_1], почта [EMAIL_1], другой [ТЕЛЕФОН_2]")

    def test_restore_after_answer(self):
        hider = privacy.Hider()
        hider.hide("Иван: мой номер +7 912 345-67-89, ИНН 7707083893")
        answer = "1. Позвонить по [ТЕЛЕФОН_1]. Иван, 22.10.2026. [00:05]\n2. Проверить [ инн_1 ] и [ТЕЛЕФОН_9]."
        self.assertEqual(
            hider.restore(answer),
            "1. Позвонить по +7 912 345-67-89. Иван, 22.10.2026. [00:05]\n2. Проверить 7707083893 и [ТЕЛЕФОН_9].",
        )

    def test_note_only_when_something_is_hidden(self):
        hider = privacy.Hider()
        self.assertEqual(hider.with_note("Ничего личного"), "Ничего личного")
        masked = hider.with_note("Телефон 8 912 345 67 89")
        self.assertTrue(masked.startswith(privacy.NOTE))
        self.assertTrue(masked.endswith("Телефон [ТЕЛЕФОН_1]"))

    def test_checksums(self):
        self.assertTrue(privacy.luhn("4111111111111111"))
        self.assertFalse(privacy.luhn("4111111111111112"))
        self.assertTrue(privacy.inn("7707083893"))
        self.assertTrue(privacy.inn("500100732259"))
        self.assertFalse(privacy.inn("1234567890"))
        self.assertFalse(privacy.inn("123"))
        self.assertTrue(privacy.snils("11223344595"))
        self.assertFalse(privacy.snils("11223344596"))
        self.assertTrue(privacy.snils("92000100000"))  # the sum 101 gives the check number 00


class TestNames(unittest.TestCase):
    def test_names_with_case_endings_become_one_person(self):
        hider = privacy.Hider(("Анна Смирнова", "Иван Петров", "Иван Сидоров"))
        hidden = hider.hide("Анна Смирнова: передайте Анне и Смирновой.\nИван Петров: Петрову и Сидорову, Ивану тоже.")
        self.assertEqual(
            hidden,
            "[ЧЕЛОВЕК_1]: передайте [ЧЕЛОВЕК_1] и [ЧЕЛОВЕК_1].\n[ЧЕЛОВЕК_2]: [ЧЕЛОВЕК_2] и [ЧЕЛОВЕК_3], Ивану тоже.",
        )  # "Иван" is shared by two people: it stays
        self.assertEqual(hider.restore("Исполнитель [ЧЕЛОВЕК_3]."), "Исполнитель Иван Сидоров.")

    def test_short_and_lowercase_words_are_not_names(self):
        hider = privacy.Hider(("Ян Ли", "Вера"))
        self.assertEqual(hider.hide("Ян Ли и вера в успех, Вере"), "[ЧЕЛОВЕК_1] и вера в успех, [ЧЕЛОВЕК_2]")
        self.assertEqual(hider.hide("Ян пришел"), "Ян пришел")  # a word of two letters alone is too ambiguous

    def test_people_of_meeting(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp) / "2026-10-22_10-00"
            folder.mkdir()
            catalog.set_(folder, calendar_participants=["Анна Смирнова", "Я"])
            transcript.write_json(folder / transcript.NAMES, {"Собеседник 1": "Олег"})
            words = [vocabulary.Word("Кузнецов", ("кузнецоф",)), vocabulary.Word("ЕГАИС"), vocabulary.Word("Облако")]
            people = privacy.people(folder, "Дмитрий", words)
        self.assertEqual(people, ("Дмитрий", "Анна Смирнова", "Олег", "Кузнецов", "кузнецоф"))

    def test_odd_calendar_data_gives_no_letters_as_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            catalog.set_(folder, calendar_participants="Анна Смирнова")  # a string instead of a list
            transcript.write_json(folder / transcript.NAMES, {"Собеседник 1": "А", "Собеседник 2": " "})
            self.assertEqual(privacy.people(folder, "", []), ())


class TestCloudAndStorage(unittest.TestCase):
    def test_cloud_or_this_computer(self):
        for address, cloud in (
            ("https://openrouter.ai/api/v1", True),
            ("https://api.proxyapi.ru/openai/v1", True),
            ("http://localhost:11434/v1", False),
            ("http://127.0.0.1:1234/v1", False),
            ("http://192.168.1.233:11434/v1", False),
            ("http://gpu-server:11434/v1", False),
            ("http://ollama.local:11434/v1", False),
            ("http://[::1]:11434/v1", False),
            ("", False),
        ):
            with self.subTest(address=address):
                self.assertEqual(privacy.cloud(address), cloud)

    def test_table_kept_in_meeting_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            privacy.remember(folder, privacy.Hider())  # nothing hidden: no file
            self.assertFalse((folder / privacy.LABELS).exists())
            hider = privacy.Hider(table=privacy.table(folder))
            hider.hide("Телефон 8 912 345 67 89")
            privacy.remember(folder, hider)
            again = privacy.Hider(table=privacy.table(folder))
            self.assertEqual(again.hide("еще 8 999 000 11 22 и 89123456789"), "еще [ТЕЛЕФОН_2] и [ТЕЛЕФОН_1]")
            self.assertEqual(privacy.restored(folder, "Звонок [ТЕЛЕФОН_1]"), "Звонок 8 912 345 67 89")


if __name__ == "__main__":
    unittest.main()
