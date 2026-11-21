
import os
from pathlib import Path

os.environ["JOBBOT_CANDIDATE_CONFIG"] = str(Path(__file__).resolve().parents[1] / "fixtures" / "candidate_profile.toml")
