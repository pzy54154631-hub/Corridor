"""Shared presentation of the five synthetic device risk questions."""

PROFILE_NAMES = {1: "Conservative", 2: "Cautious", 3: "Balanced", 4: "Growth", 5: "Aggressive"}
ANSWER_OPTIONS = {
    "Investment experience": (
        "None",
        "Savings only",
        "Simple funds",
        "Several product types",
        "Complex products",
    ),
    "Loss tolerance": (
        "None",
        "A small loss",
        "A moderate loss",
        "A substantial loss",
        "A large loss",
    ),
    "Income stability": ("No stable income", "Variable", "Mostly stable", "Stable", "Very stable"),
    "Liquidity needs": (
        "Immediate access",
        "Frequent access",
        "Occasional access",
        "Rare access",
        "No near-term need",
    ),
    "Investment horizon": ("Immediate", "Short term", "Medium term", "Long term", "Very long term"),
}
