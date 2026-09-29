"""What Aurelia may learn about Kentrell from a conversation, before anything reaches the mailbox."""
from app import mnemosyne, profile

RULES = {**mnemosyne.DEFAULT_RULES}


def obs(**kw):
    return {"fact": "Likes short answers in the morning", "category": "communication", "core": True,
            "why": "asked twice for 'just the short version'", "confidence": 0.8, "replaces": None, **kw}


def test_well_formed_confident_observations_pass():
    [o] = profile.parse_observations([obs()], RULES, set())
    assert o == {"fact": "Likes short answers in the morning", "category": "communication", "core": True,
                 "why": "asked twice for 'just the short version'", "confidence": 0.8, "replaces": None}


def test_filters():
    raw = [obs(confidence=0.3),                                     # not sure enough
           obs(category="health", fact="Seems to sleep badly"),     # never inferred (policy)
           obs(category="astrology"),                               # not a category
           obs(fact=""), "not an object", obs(fact="x" * 400),
           obs(replaces=99),                                        # updates a fact that doesn't exist: new instead
           obs(replaces=7, fact="Likes short answers, mornings and nights")]
    kept = profile.parse_observations(raw, {**RULES, "learn_categories": [*profile.CATEGORIES, "health"]}, {7})
    assert [(o["fact"], o["replaces"]) for o in kept] == [("Likes short answers in the morning", None),
                                                         ("Likes short answers, mornings and nights", 7)]
    assert profile.parse_observations([obs()] * 9, RULES, set()).__len__() == 5          # max per conversation
    assert profile.parse_observations({"not": "a list"}, RULES, set()) == []


def test_mood_patterns_are_allowed_unless_the_policy_says_otherwise():
    mood = obs(category="mood", fact="Prefers lighter, playful conversation late at night")
    assert profile.parse_observations([mood], RULES, set())
    assert not profile.parse_observations([mood], {**RULES, "never_infer": ["health", "mood"]}, set())


def test_the_brief_lists_what_she_knows_and_what_he_declined():
    brief = profile.learning_brief(RULES, [{"id": 3, "fact": "Vegetarian"}], ["Dislikes jazz"])
    assert "3: Vegetarian" in brief and "- Dislikes jazz" in brief and "Never propose anything about: health" in brief
    assert "never a one-off mood, never a diagnosis" in brief


def test_the_same_idea_has_the_same_fingerprint():
    assert profile.fingerprint("Likes short answers.") == profile.fingerprint("likes  SHORT answers")
    assert profile.fingerprint("Likes short answers") != profile.fingerprint("Likes long answers")
    assert profile.category("PEOPLE") == "people" and profile.category("nonsense") == "general"
