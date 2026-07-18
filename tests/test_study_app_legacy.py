"""Coverage for study_app.py's legacy-redirect behavior.

StudyApp used to be a standalone flashcard UI that wrote a plain learned=1
flag directly, bypassing the SM-2 scheduler now used everywhere else. It's
retained in the source only for reference; running study_app.py must open
the primary dashboard instead, and StudyApp itself must refuse to run if
anything ever tries to instantiate it directly.
"""

import runpy
import unittest
from unittest import mock

import study_app


class StudyAppLegacyGuardTests(unittest.TestCase):
    def test_instantiating_studyapp_directly_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            study_app.StudyApp(root=None)
        self.assertIn("dashboard_app.py", str(ctx.exception))

    def test_running_study_app_as_main_opens_the_dashboard(self):
        fake_app = mock.Mock()
        fake_dashboard_cls = mock.Mock(return_value=fake_app)

        with mock.patch("dashboard_app.DashboardApp", fake_dashboard_cls):
            runpy.run_path(study_app.__file__, run_name="__main__")

        fake_dashboard_cls.assert_called_once_with()
        fake_app.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
