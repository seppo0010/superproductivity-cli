import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sp_telegram import subte

# Trimmed from a real Emova `estadoLineas` payload (2026-09-25).
STATUS_HTML = """
<div class="row filaEncabezadoEstadoServicio">
    <div class="col estadoServicio"><h3>Estado del servicio</h3></div>
    <div class="col fecha"><div class="float-end">25. 09. 2026 </div></div>
</div>
<div class="row">
    <div class="col"><div class="d-flex justify-content-center itemLinea"><div>
        <img src="https://emova.com.ar/past-a-60.png" alt="Linea A">
        <p>Normal</p>
    </div></div></div>
    <div class="col"><div class="d-flex justify-content-center itemLinea"><div>
        <img src="https://emova.com.ar/past-b-60.png" alt="Linea B">
        <p>Estación Medrano cerrada por obras. Viernes y Sábado con horario extendido.</p>
    </div></div></div>
    <div class="col"><div class="d-flex justify-content-center itemLinea"><div>
        <img src="https://emova.com.ar/past-p-60.png" alt="Linea Premetro">
        <p>Normal</p>
    </div></div></div>
</div>
"""


class ParseStatusHtml(unittest.TestCase):
    def test_parses_each_line(self):
        self.assertEqual(subte._parse_status_html(STATUS_HTML), {
            "A": "Normal",
            "B": "Estación Medrano cerrada por obras. Viernes y Sábado con horario extendido.",
            "Premetro": "Normal",
        })


class ParseSubteTag(unittest.TestCase):
    def test_single_line(self):
        self.assertEqual(subte.parse_subte_tag("subte: D"), ["D"])

    def test_multiple_lines_case_insensitive(self):
        self.assertEqual(subte.parse_subte_tag("Subte: d, c"), ["D", "C"])

    def test_premetro(self):
        self.assertEqual(subte.parse_subte_tag("subte: premetro"), ["Premetro"])

    def test_no_tag(self):
        self.assertEqual(subte.parse_subte_tag("Llevar apuntes"), [])
        self.assertEqual(subte.parse_subte_tag(""), [])

    def test_tag_among_other_lines(self):
        self.assertEqual(subte.parse_subte_tag("Llevar apuntes\nsubte: D\nAula 3"), ["D"])

    def test_html_description(self):
        self.assertEqual(subte.parse_subte_tag("Llevar apuntes<br>subte: D<br>Aula 3"), ["D"])

    def test_mention_mid_sentence_is_not_a_tag(self):
        self.assertEqual(subte.parse_subte_tag("ir en subte: D o colectivo"), [])


class Classify(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(subte.classify("Normal"), "ok")

    def test_delays(self):
        self.assertEqual(subte.classify("Servicio con demoras"), "problem")

    def test_interrupted(self):
        self.assertEqual(subte.classify("Servicio interrumpido entre Catedral y Palermo"), "problem")

    def test_strike(self):
        self.assertEqual(subte.classify("Sin servicio por paro"), "problem")

    def test_informational_note(self):
        self.assertEqual(subte.classify("Estación Medrano cerrada por obras."), "ok")

    def test_unknown_line(self):
        self.assertEqual(subte.classify(None), "problem")


def _event(start, description="subte: D"):
    return {"title": "Travel to Derecho", "start": start, "end": start, "all_day": False,
            "busy": True, "description": description, "uid": "abc"}


class CheckSubteTrips(unittest.TestCase):
    START = datetime(2026, 9, 25, 14, 30)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state_dir = Path(self.tmp.name)
        for p in (
            patch.object(subte.config, "STATE_DIR", state_dir),
            patch.object(subte.config, "SUBTE_STATE_FILE", state_dir / "subte_state.json"),
            patch.object(subte.config, "CHAT_ID", "1"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.events = [_event(self.START)]
        p = patch.object(subte.ical, "_calendar_events_for_day", side_effect=lambda *a, **k: self.events)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p = patch.object(subte, "_telegram_call", side_effect=lambda m, **kw: self.sent.append(kw["text"]))
        p.start()
        self.addCleanup(p.stop)

    def run_at(self, hh, mm, statuses):
        fetch = (patch.object(subte, "fetch_line_statuses", side_effect=statuses)
                 if isinstance(statuses, Exception)
                 else patch.object(subte, "fetch_line_statuses", return_value=statuses))
        with fetch as mock_fetch:
            result = subte.check_subte_trips(now=datetime(2026, 9, 25, hh, mm))
        return result, mock_fetch

    def test_before_window_returns_seconds_to_pre_check(self):
        result, mock_fetch = self.run_at(13, 0, {"D": "Normal"})
        self.assertEqual(result, 60 * 60 + 1)
        mock_fetch.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_ok_at_pre_check_is_silent_then_ok_at_departure(self):
        result, _ = self.run_at(14, 0, {"D": "Normal"})
        self.assertEqual(self.sent, [])
        self.assertEqual(result, 30 * 60 + 1)
        result, _ = self.run_at(14, 30, {"D": "Normal"})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("✅", self.sent[0])
        self.assertIsNone(result)

    def test_problem_at_pre_check_alerts_once(self):
        self.run_at(14, 0, {"D": "Servicio con demoras"})
        self.run_at(14, 5, {"D": "Servicio con demoras"})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("🚨", self.sent[0])
        self.assertIn("colectivo", self.sent[0])

    def test_problem_persisting_at_departure(self):
        self.run_at(14, 0, {"D": "Servicio con demoras"})
        self.run_at(14, 31, {"D": "Servicio con demoras"})
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Sigue", self.sent[1])

    def test_informational_note_is_shown_with_ok(self):
        self.run_at(14, 30, {"D": "Mañana horario extendido."})
        self.assertIn("✅", self.sent[0])
        self.assertIn("horario extendido", self.sent[0])

    def test_fetch_failure_warns(self):
        self.run_at(14, 0, ValueError("boom"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("No pude verificar", self.sent[0])

    def test_past_grace_sends_nothing(self):
        result, mock_fetch = self.run_at(15, 0, {"D": "Normal"})
        self.assertEqual(self.sent, [])
        mock_fetch.assert_not_called()
        self.assertIsNone(result)

    def test_untagged_event_ignored(self):
        self.events = [_event(self.START, description="Llevar apuntes")]
        result, mock_fetch = self.run_at(14, 30, {"D": "Normal"})
        self.assertEqual(self.sent, [])
        mock_fetch.assert_not_called()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
