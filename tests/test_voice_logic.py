import unittest

from app.routes.voice import _needs_filler


class VoiceLogicTests(unittest.TestCase):
    def test_fast_path_name_and_time_do_not_need_filler(self):
        self.assertFalse(_needs_filler("Mario Rossi", "collecting_name"))
        self.assertFalse(_needs_filler("alle 8 e mezza", "collecting_pickup_time"))

    def test_mixed_name_or_time_messages_still_use_filler(self):
        self.assertTrue(_needs_filler("aggiungi una margherita", "collecting_name"))
        self.assertTrue(_needs_filler("alle 8 e aggiungi una margherita", "collecting_pickup_time"))


if __name__ == "__main__":
    unittest.main()
