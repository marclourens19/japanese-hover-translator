import logging
from pathlib import Path
import tempfile
import unittest
import uuid

from app_logging import LOG_FILE_NAME, configure_logging


class LoggingTests(unittest.TestCase):
    def test_file_log_rotates_and_retains_bounded_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            logger, log_path = configure_logging(
                directory,
                logger_name="jht-test-" + uuid.uuid4().hex,
                max_bytes=300,
                backup_count=2,
                console=False,
            )
            for index in range(40):
                logger.info("rotation record %02d %s", index, "x" * 60)
            for handler in logger.handlers:
                handler.flush()

            log_directory = Path(directory) / "logs"
            self.assertEqual(log_path.name, LOG_FILE_NAME)
            self.assertTrue(log_path.is_file())
            self.assertTrue(Path(str(log_path) + ".1").is_file())
            self.assertLessEqual(
                len(list(log_directory.glob(LOG_FILE_NAME + "*"))),
                3,
            )

            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            logging.Logger.manager.loggerDict.pop(logger.name, None)


if __name__ == "__main__":
    unittest.main()
