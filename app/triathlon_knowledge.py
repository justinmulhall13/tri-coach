"""What the best long-course triathletes do, and how much of it is evidence.

Coach Steve's triathlon reference, the sport counterpart of strength_knowledge.
Each topic says what the pros do, what the research supports, how it applies to
this athlete and what not to copy. Reviewed 2026-11 from published studies, race
fuelling case studies and coach write-ups; the README lists the sources.

Two limits are built in. A pro's principle scales to an age-grouper and a pro's
numbers do not: nothing here is a prescription, and the athlete's own rules in
the coach prompt (bike by heart rate, easy ceilings, his fuelling numbers, the
run cap) outrank every line. And nothing here is about his body: his numbers
come from his own data, never from a table of what a world champion eats.

The second half is the night-out check. "Can I go out" gets a deterministic
answer from the race date and the stored plan, so how strict the coach is
depends on the calendar and not on the model's mood: a flat no inside two weeks
of the race, a no or a trade the night before a key session once the block is
past base, and a plain yes when nothing important sits behind the night.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

REVIEWED = "2026-11"

HOW_TO_USE = (
    "Background for your reasoning, not a script. Give him the decision in a sentence. "
    "Name a pro or a study only when he asks why or asks what the pros do. His own rules "
    "and numbers outrank every line here, and none of these figures is his."
)

# ------------------------------------------------------------------- topics

TOPICS: dict[str, dict[str, Any]] = {
    "intensity": {
        "title": "Intensity distribution and controlled threshold work",
        "evidence": "Strong for the easy volume. Unsettled for the best mix of hard work.",
        "pros": [
            "The Norwegian group (Blummenfelt, Iden, Stornes, coached by Olav Aleksander Bu) "
            "trains a very high volume at low intensity and does its hard work as threshold "
            "intervals held under a lactate cap, often two such sessions in one day. They "
            "finished first, second and third at the 2025 Ironman World Championship in Nice.",
            "A published case study of a world-class triathlete's build to the Tokyo Olympics "
            "logged about four fifths of training time below the first threshold.",
        ],
        "research": [
            "Across endurance sports, elite athletes spend roughly 80% or more of training "
            "time below the first threshold.",
            "Whether the rest is better pyramidal (more threshold) or polarized (more VO2max "
            "work) is not settled. The constants are the easy volume and hard work controlled "
            "enough to repeat.",
        ],
        "for_him": [
            "This is rule 6 with a cap on it, not an exception to it: threshold intervals are "
            "a hard day, done as repeats that finish with one more in the tank.",
            "He has no lactate meter. Control comes from a heart-rate cap and from "
            "repeatability: the last rep matches the first, and the session ends when it would not.",
            "Easy days stay under their ceiling. That is where most of the gain is and where "
            "he most often goes wrong.",
        ],
        "do_not": [
            "Double-threshold days or pro volume. Both depend on lactate testing and a "
            "training load he does not have.",
            "Letting easy sessions drift up because a moderate effort feels productive.",
        ],
    },
    "swim": {
        "title": "Swimming for an adult-onset swimmer",
        "evidence": "Mostly coaching practice, not trials.",
        "pros": [
            "Coaches of adult-learned swimmers favour frequency over session length: three or "
            "four shorter swims beat two long ones, because the skill fades between sessions.",
            "Technique comes before fitness: breathing built into the stroke rhythm, body "
            "position, then pace. Video of his own stroke is the fastest feedback there is.",
            "Threshold pace is anchored with a critical swim speed test: a 400 m and a 200 m "
            "time trial in one session. CSS pace per 100 m is the 400 time minus the 200 time, "
            "divided by 2. Retest every six to eight weeks.",
            "A 1.9 km race swim is paced close to CSS, a little slower. Sighting, sitting on "
            "feet and a controlled start are practised, not assumed.",
        ],
        "research": [
            "Improvement in adult-onset swimmers is slow: months to years, not weeks.",
        ],
        "for_him": [
            "The swim is his weakest leg. Three swims a week are protected and are never "
            "traded for more bike or run.",
            "Never invent his CSS. With no test on record, schedule the 400 and the 200 and "
            "work from the result.",
        ],
        "do_not": [
            "Long, tired, sloppy swims. They rehearse the stroke he is trying to lose.",
        ],
    },
    "bike": {
        "title": "Pacing the 70.3 bike",
        "evidence": "Moderate: race-file analysis and modelling, not controlled trials.",
        "pros": [
            "Long-course bike effort is set by how long the ride will take. For most "
            "age-groupers a 70.3 bike lands around 70 to 78% of threshold power; stronger "
            "riders sit higher.",
            "The best-paced rides are flat in effort: a variability index under about 1.05, "
            "with no surges over hills or out of corners.",
            "Age-group race files often show the first hour 5 to 12% harder than the second. "
            "That fade is a pacing mistake, and the run pays for it.",
        ],
        "research": [
            "Reviews of pacing find that in events longer than a couple of minutes an evenly "
            "spread effort gives the best time. That comes from time trials in general: the "
            "bike leg of a triathlon has been studied far less.",
        ],
        "for_him": [
            "He rides by heart rate, never watts (rule 7). So this becomes: steady effort, "
            "lap-average heart rate inside the cap, and deliberately easy for the first 20 to "
            "30 minutes while heart rate is still high from the swim.",
            "Climbs are ridden at the same effort, not the same speed.",
            "Long rides are where the race position and the race fuelling get practised.",
            "The bike is paced for the run. Getting off feeling it was slightly too easy is "
            "the right result.",
        ],
        "do_not": [
            "Chasing a speed or another rider.",
            "Skipping a feed to hold a position.",
        ],
    },
    "run": {
        "title": "The run off the bike, and durability",
        "evidence": "The idea is well supported. What trains it best is not.",
        "pros": [
            "Long-course races are decided late. Durability is the ability to hold pace as "
            "fatigue builds, and it separates athletes with the same fresh fitness.",
            "The usual field check is drift on a long steady session: heart rate climbing "
            "against a held pace or effort. Coaches treat under about 5% as solid.",
        ],
        "research": [
            "The levers coaches use are volume built over months, race-pace work placed late "
            "in long sessions, bricks, and carbohydrate during long work.",
            "Low carbohydrate availability and heat both make the fade worse.",
        ],
        "for_him": [
            "The run is raced on legs the bike already spent. The first two to three "
            "kilometres go slower than goal pace, and the pace is allowed to come.",
            "Run volume grows about 10% a week (rule 8). Durability comes from the long run "
            "and the long ride arriving every week, not from one heroic session.",
            "Long sessions are fuelled. Doing them short of carbohydrate trains the fade.",
        ],
        "do_not": [
            "Racing the first kilometre out of T2.",
            "Adding run volume in a jump to chase durability.",
        ],
    },
    "brick": {
        "title": "Bricks and the race rehearsal",
        "evidence": "Coaching practice.",
        "pros": [
            "Short runs off the bike teach the transition; they do not need to be long to "
            "work. Longer bricks at race effort belong to the specific phase, and one full "
            "rehearsal sits in the peak.",
        ],
        "research": [],
        "for_him": [
            "Bricks begin in build (rule 10). Each one rehearses three things: the pace of "
            "the first kilometre, the fuelling, and the transition itself.",
            "The rehearsal uses the race kit, the race fuel and the race pacing. Whatever "
            "goes wrong there is the point of doing it.",
        ],
        "do_not": [
            "Turning every brick into a race. The run off the bike is controlled.",
        ],
    },
    "fuel": {
        "title": "Race fuelling and training the gut",
        "evidence": "Strong that carbohydrate helps. Individual on how much is tolerated.",
        "pros": [
            "Reported intakes at the front of 70.3 racing run from about 90 to 140 g of "
            "carbohydrate an hour, most of it on the bike: Solveig Lovseth about 139 g/h at "
            "Oceanside in 2026, Jelle Geens about 98 g/h winning the 2025 70.3 world title.",
            "Published bike targets among the top men range from about 120 to 200 g/h.",
        ],
        "research": [
            "General guidance for long events is 60 to 90 g/h from mixed glucose and "
            "fructose. Athletes who train the gut tolerate more, toward 120 g/h.",
            "Tolerance is trained: weeks of practising race intake in long sessions.",
        ],
        "for_him": [
            "His own fuelling numbers and ceilings govern, and `fueling_reference` carries "
            "them. A pro's intake is context, never a target.",
            "Any increase is earned in training first, on long rides, one step at a time. He "
            "raises a ceiling. The coach does not.",
            "Most of the carbohydrate goes in on the bike and the run takes less. Feeding "
            "starts early and runs on a timer, not on hunger.",
            "Day to day, carbohydrate follows the work: key sessions are fuelled, and nothing "
            "hard or long is done fasted.",
        ],
        "do_not": [
            "Trying a higher intake for the first time on race day. More only helps if it "
            "stays down.",
        ],
    },
    "heat": {
        "title": "Heat training",
        "evidence": "Moderate for racing in the heat. Early for anything else.",
        "pros": [
            "Many pros now run heat blocks, often where altitude camps used to sit.",
        ],
        "research": [
            "Five weeks of heat-suit training raised haemoglobin mass in elite cross-country "
            "skiers by about 30 g over controls, without a clear performance difference.",
            "A 2025 review calls blocks of five weeks or more promising for oxygen-carrying "
            "capacity.",
            "The established benefit is narrower: one to two weeks of heat exposure before a "
            "hot race improves performance in that heat.",
        ],
        "for_him": [
            "Victoria in late May is usually mild, so this is optional and late. Check the "
            "forecast before spending recovery on it.",
            "A heat session is load. It is an easy session made harder, never stacked on a "
            "hard day or a short night.",
        ],
        "do_not": [
            "Using it to lose weight, or finishing dehydrated on purpose.",
        ],
    },
    "supplements": {
        "title": "Supplements, including bicarbonate",
        "evidence": "Good for a short list. Weak for everything else.",
        "pros": [
            "Sodium bicarbonate in hydrogel form is now common in pro racing.",
        ],
        "research": [
            "The sports-nutrition consensus backs a short list: caffeine, creatine, nitrate, "
            "beta-alanine and sodium bicarbonate. Most other products have little behind them.",
            "Bicarbonate is best supported for hard efforts of one to ten minutes. One "
            "manufacturer-funded study of 14 trained cyclists found a 40 km time trial about "
            "1.4% faster with little gut trouble. There is no triathlon trial, and gut "
            "distress is the known cost.",
            "Caffeine works at 3 to 6 mg per kg about an hour before, and often at less.",
            "Creatine helps strength work and usually adds a kilo or two of water weight, "
            "which a triathlete carries up every hill.",
        ],
        "for_him": [
            "Low priority next to sleep, fuelling and consistency. Discuss it when he raises it.",
            "His gels already carry caffeine: count that before adding any.",
            "Anything new is trialled in training several times, and only third-party tested "
            "products. Nothing new in race week.",
        ],
        "do_not": [
            "First use of anything on race day.",
            "Working out a dose from a body weight that is not on record.",
        ],
    },
    "sleep": {
        "title": "Sleep",
        "evidence": "Strong that restriction hurts. Thin on banking sleep.",
        "pros": [
            "Pros treat sleep as part of the training day and nap around hard sessions.",
        ],
        "research": [
            "Three nights at about four hours cut elite cyclists' time to exhaustion by "
            "about 10%.",
            "Extending sleep toward eight and a half hours improved sprint times and "
            "accuracy in college basketball players.",
            "There is no direct evidence that sleep can be banked before a race. One poor "
            "night before race day matters less than the week before it.",
        ],
        "for_him": [
            "Sleep is the first recovery tool and it is free. In the last two weeks it is "
            "protected like a key session.",
            "One short night is not a crisis: the next morning goes easy or flexible, and "
            "quality is not forced onto it.",
        ],
        "do_not": [
            "Making up a lost night by training harder.",
        ],
    },
    "alcohol": {
        "title": "Alcohol and recovery",
        "evidence": "Good that the cost grows with the dose and lands on sleep and recovery.",
        "pros": [
            "Most pros drink little in season and not at all near a race, and plenty of them "
            "have a beer after one.",
        ],
        "research": [
            "After a hard session a heavy dose (about 1.5 g per kg) cut muscle protein "
            "synthesis by about a quarter even when taken with protein, and by more with "
            "carbohydrate alone.",
            "In about 4,000 adults, overnight heart-rate-variability recovery fell by about "
            "9, 24 and 39 percentage points after a light (up to 0.25 g per kg), moderate and "
            "heavy (over 0.75 g per kg) intake. The effect was larger in younger people and "
            "being fit did not protect.",
            "Even small amounts delay REM sleep. In one wearable data set of college "
            "athletes a heavy night showed in recovery for several days.",
        ],
        "for_him": [
            "Dose and timing are the levers. A night with nothing important behind it costs "
            "little. The same night before a key session, or inside two weeks of the race, "
            "costs the session or the taper.",
            "`night_out_check` says which kind of night it is.",
            "The morning after: easy and short, or rest. Nothing hard or long, and no trying "
            "to sweat it out.",
        ],
        "do_not": [
            "Naming a number of drinks to aim for.",
            "Moralising, or claiming a hangover is certain.",
        ],
    },
    "taper": {
        "title": "The taper and race week",
        "evidence": "Strong: several meta-analyses agree.",
        "pros": [
            "Pros cut volume and keep the sharp work: short race-pace efforts right up to "
            "race week, with the long sessions shrinking.",
        ],
        "research": [
            "About two weeks, volume cut progressively by 41 to 60%, intensity and frequency "
            "kept. The gain is a few percent, and it is larger after a hard overload block.",
            "Tapers of three weeks or less performed best.",
            "For events over 90 minutes, carbohydrate loading is 10 to 12 g per kg a day for "
            "the last 36 to 48 hours, from low-fibre food he already eats.",
        ],
        "for_him": [
            "His contract sets the target: race-day form of +5 to +15, and above +20 means he "
            "arrived undertrained.",
            "Volume comes down, the short race-pace touches stay, and long sessions may sit "
            "up to eight days out.",
            "Race week: nothing new. Not kit, not food, not a session.",
        ],
        "do_not": [
            "Resting completely.",
            "Testing fitness in race week.",
        ],
    },
    "race": {
        "title": "Executing a 70.3",
        "evidence": "Coaching practice and race-file analysis.",
        "pros": [
            "Swim: a controlled start, settled inside the first 200 m, sighting often, on "
            "feet whenever feet are there.",
            "T1 and T2 are free time, walked through beforehand.",
            "Bike: easy for the first 20 to 30 minutes, steady after, feeding on a timer from "
            "the start.",
            "Run: the first two to three kilometres slower than goal pace, then hold. The "
            "aid-station routine is decided before the gun.",
        ],
        "research": [],
        "for_him": [
            "Pace comes from his goal splits and heart-rate caps in the contract, never from "
            "a pro's file.",
            "Everything on race day was done in the peak rehearsal first.",
        ],
        "do_not": [
            "Racing the swim start.",
            "Banking time on the bike.",
        ],
    },
    "recovery": {
        "title": "Missed sessions, illness and recovery weeks",
        "evidence": "Coaching practice.",
        "pros": [
            "Pros protect consistency over any single session. A missed session is dropped, "
            "not crammed into the next day.",
            "Recovery weeks are part of the plan, not a reward. Here that is every fourth week.",
            "Illness: symptoms above the neck allow easy work. Fever, chest symptoms or body "
            "aches mean rest, and the return starts with easy days.",
        ],
        "research": [],
        "for_him": [
            "A missed easy session is let go. A missed key session moves inside the week if "
            "the spacing still works; otherwise it is dropped and the next one is kept.",
            "Two missed key sessions in a week is a replan, said plainly. It is not a guilt trip.",
        ],
        "do_not": [
            "Doubling up to catch up.",
            "Treating one bad day as a trend.",
        ],
    },
    "strength": {
        "title": "Lifting alongside triathlon training",
        "evidence": "Good in trained endurance athletes.",
        "pros": [
            "Most long-course pros lift through the season and cut the volume, not the load, "
            "as races approach.",
        ],
        "research": [
            "Heavy, low-rep strength work alongside endurance training improves running and "
            "cycling economy without adding much mass.",
        ],
        "for_him": [
            "Rule 11 and the lifting rules govern, and `physio_reference` handles pain and "
            "substitutions.",
            "A lift never lands on, or the morning before, a key endurance session.",
        ],
        "do_not": [
            "Lifting to failure inside the specific phase or later.",
        ],
    },
}

# Topics that only make sense while the active event is a triathlon.
TRI_ONLY = frozenset({"swim", "bike", "run", "brick", "race"})

_CUE_RE = re.compile(
    r"how (?:do|should|can|hard|fast|long|much|many|often)|faster|quicker|improve|better|"
    r"pac(?:e|ing)|effort|technique|strategy|tips?\b|advice|what should|"
    r"why (?:am|do|does|is)|weak|struggl|slow|mistake|wrong",
    re.I,
)

# (topic, always, subject). `always` routes on its own; `subject` only together
# with a how-to cue, so "move my swim to Tuesday" does not drag the swim
# reference into a scheduling turn.
_ROUTES: tuple[tuple[str, re.Pattern[str] | None, re.Pattern[str] | None], ...] = (
    ("alcohol", None, None),            # routed by mentions_alcohol, below
    ("sleep", re.compile(
        r"\bsleep\w*|\bslept\b|\bnaps?\b|napping|insomnia|bed ?time|jet ?lag|"
        r"all[- ]nighter|stay(?:ing|ed)? up|late night|(?:up|out) late", re.I), None),
    ("taper", re.compile(
        r"\btaper\w*|race week|carb[- ]?load\w*|last (?:two|2|three|3) weeks|sharpen\w*|"
        r"peak (?:week|block|phase)", re.I), None),
    ("race", re.compile(
        r"race day|race morning|race plan|race strategy|race execution|"
        r"how (?:should|do) i race|game ?plan|pacing (?:plan|strategy)|transitions?\b|"
        r"\bt[12]\b", re.I), None),
    ("fuel", re.compile(
        r"\bg/h\b|grams? (?:per|an) hour|carbs? (?:per|an) hour|gut[- ]train\w*|"
        r"train(?:ing)? (?:my|the) gut|high[- ]carb|how (?:much|many) (?:carbs?|gels?|sodium)|"
        r"race (?:nutrition|fuel\w*)|fuel\w* (?:plan|strategy)|bonk\w*|fasted|"
        r"what (?:do|should) (?:the )?pros eat", re.I),
     re.compile(r"fuel\w*|carb\w*|\bgels?\b|nutrition|sodium|electrolytes?|hydrat\w+", re.I)),
    ("swim", re.compile(
        r"\bcss\b|critical swim|open[- ]water|wetsuit|sighting|"
        r"swim (?:technique|form|stroke|drills?|test|pac\w+|speed|fitness|faster)|"
        r"(?:faster|better|quicker) (?:at |in the |in my )?swim\w*|bilateral", re.I),
     re.compile(r"\bswim\w*|\bpool\b|\bstroke\b|freestyle|front crawl", re.I)),
    ("bike", re.compile(
        r"bike (?:pac\w+|leg|split|effort|strategy|fitness)|pac\w+ (?:the |my )?(?:bike|ride)|"
        r"variability index|normali[sz]ed power|aero (?:position|bars?)|intensity factor|"
        r"how hard (?:should|do|can) i (?:ride|bike)", re.I),
     re.compile(r"\b(?:bike|biking|ride|riding|cycling)\b", re.I)),
    ("run", re.compile(
        r"off the bike|durab\w+|decoupl\w+|cardiac drift|(?:heart rate|hr) drift|"
        r"run (?:leg|split|pac\w+|strategy)|pac\w+ (?:the |my )?run|\bfad(?:e|es|ed|ing)\b|"
        r"blow(?:ing|s)? up|hit(?:ting)? the wall", re.I),
     re.compile(r"\brun(?:s|ning)?\b", re.I)),
    ("brick", re.compile(r"\bbricks?\b|transition run|\brehearsal\b", re.I), None),
    ("intensity", re.compile(
        r"norwegian|double[- ]threshold|lactate|polari[sz]ed|pyramidal|80[ /:-]?20|"
        r"sweet ?spot|intensity distribution|gr[ae]y zone|zone ?2|\bz2\b|"
        r"how (?:do|should) (?:the )?pros|like (?:a|the) pros?", re.I),
     re.compile(
        r"threshold|tempo|intervals?|easy (?:days?|runs?|rides?|pace|sessions?)|intensity|"
        r"hard (?:days?|sessions?)", re.I)),
    ("heat", re.compile(
        r"\bheat\b|\bsauna\b|heat[- ]suit|acclimat\w+|humid\w*|core temp\w*|overheat\w*|"
        r"hot (?:race|day|weather|conditions)", re.I), None),
    ("supplements", re.compile(
        r"bicarb\w*|baking soda|beta[- ]alanine|\bketones?\b|supplements?|creatine|"
        r"nitrates?|beet(?:root)? juice|pre[- ]workout|caffeine (?:dose|pills?|tablets?)",
        re.I), None),
    ("recovery", re.compile(
        r"miss(?:ed|ing)? (?:a |an |my |the |that |today'?s |yesterday'?s )?(?:\w+ )?"
        r"(?:session|workout|run|ride|swim|lift|day|week)|skip(?:ped|ping)?\b|"
        r"make (?:it|that|them) up|catch(?:ing)? up|fell behind|behind on|\bsick\b|\bill\b|"
        r"illness|a cold\b|\bflu\b|fever|sore throat|overtrain\w*|burn(?:ed|t)? ?out|deload|"
        r"recovery week|rest day|day off", re.I), None),
    ("strength", re.compile(
        r"strength (?:training |work )?for (?:tri\w*|endurance|running|cycling|swimming)|"
        r"should i (?:still |keep )?lift\w*", re.I), None),
)

# ------------------------------------------------------------ night-out terms

_DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?"
_CREW = (r"(?:the |my )?(?:boys|guys|friends|mates|roommates|team|girls|lads|girlfriend|"
         r"boyfriend)")

_ALCOHOL_RE = re.compile(
    r"\b(?:beers?|booze|boozing|alcohol|liquor|tipsy|tequila|vodka|whisk(?:e)?y|wine|"
    r"drunk(?! (?:\d|a |any |enough|much|water|too little))|"
    r"cocktails?|ciders?|hard seltzers?|white claws?|pre-?gam(?:e|ing)|pre-?drinks?|"
    r"keg(?:ger)?|bender|pubs?|nightclub|clubbing|bar ?crawl|pub ?crawl|beer pong|"
    r"happy hour|(?:take|taking|took|do|doing|did) (?:a few |some )?shots|"
    r"(?:to|at|hit(?:ting)?) (?:the|a) bars?)\b",
    re.I,
)
# The morning after: routes the alcohol reference, never the night-out check.
_AFTERMATH_RE = re.compile(
    r"\bhung ?over\b|\bhangover\b|\bdrank\b|\bgot (?:drunk|wasted|hammered)\b", re.I)

# "Drink" is a fuelling word far more often than an alcohol one in this app, so
# it only counts beside a social cue and never beside a bottle or a session.
_DRINK_WORD_RE = re.compile(r"\bdrink(?:s|ing)?\b", re.I)
_FUEL_CONTEXT_RE = re.compile(
    r"water|coffee|\btea\b|milk|\bmix\b|electrolytes?|sodium|\bsalt|carb|fluids?|gatorade|"
    r"bottles?|hydrat|thirst|\bgels?\b|protein|shake|smoothie|juice|\bml\b|lit(?:re|er)s?\b|"
    r"(?:sports?|energy|recovery) drinks?|during|while (?:riding|running|racing)|"
    r"on (?:the |my |a |long )*(?:bike|ride|run|trainer|peloton)s?\b|(?:per|an|every) hour|"
    r"aid stations?|how (?:much|often) (?:should|do|must) i drink|"
    r"what (?:should|do|can|could) i drink",
    re.I,
)
_SOCIAL_RE = re.compile(
    r"tonight|tonite|tomorrow|weekend|\b(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day|fri|"
    r"thurs?|tues?)\b|\bnight\b|\bout\b|friends|boys|guys|mates|roommates|birthday|wedding|"
    r"dinner|\b(?:can|could|should|may) i\b|\bok(?:ay)? (?:if|to)\b|allowed|fine (?:if|to)|"
    r"a few|a couple|couple of|some drinks|(?:have|grab|get) a drink|for (?:a )?drinks?",
    re.I,
)
_PARTY_RE = re.compile(
    r"\b(?:part(?:y|ies|ying)|after[- ]?party|kickback|rave|night out|big night|"
    r"go(?:ing)? out (?:again )?(?:tonight|tonite|tomorrow|this weekend|drinking|partying|"
    r"and (?:drink|party)|for (?:a )?(?:drinks?|beers?)|(?:on )?" + _DAY + r"\b|with " + _CREW + r")|"
    r"(?:can|could|should|may) i (?:still )?go out\b"
    r"(?! (?:too|for a|for my|on the|on my|hard|fast|easy|and (?:run|ride|swim)))|"
    r"out (?:tonight|with " + _CREW + r"))\b",
    re.I,
)
_LATE_RE = re.compile(
    r"\b(?:stay(?:ing)? (?:up|out)(?: late)?|up (?:late|all night)|late night|all[- ]nighter|"
    r"out late|past midnight|(?:until|till|til) [1-5] ?a\.?m\b|concert|festival|wedding|gala|"
    r"banquet)",
    re.I,
)
# A night that already happened is not a question about tonight.
_PAST_RE = re.compile(
    r"\blast night\b|\byesterday\b|\blast (?:weekend|week|" + _DAY + r")\b|"
    r"\b(?:had|got|was|were|went|stayed)\b(?:\s+\w+){0,3}?\s+"
    r"(?:late night|big night|night out|drunk|wasted|hammered|out late|up late|up all night|"
    r"too many|too much)\b|"
    r"\b(?:drank|partied|stayed (?:up|out)|went out|was (?:out|up) (?:late|all night)|"
    r"(?:was|been) drinking)\b",
    re.I,
)
_AHEAD_RE = re.compile(
    r"\b(?:tonight|tonite|this evening|tomorrow|tmrw|this weekend|next|again|going to|gonna|"
    r"planning|plan (?:to|on)|thinking (?:about|of))\b",
    re.I,
)
# "or Saturday", "what about Sunday instead": another night offered for the same question.
_FOLLOW_RE = re.compile(
    r"\b(?:or|instead|rather|better|what about|how about|what if)\b", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"[?!;,\n]+|\.(?=\s|$)")

# ---------------------------------------------------------- which night it is

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_INDEX = {name[:3].lower(): i for i, name in enumerate(_WEEKDAYS)}
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_WORD_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

_TONIGHT_RE = re.compile(r"\b(?:tonight|tonite|this evening)\b", re.I)
_TOMORROW_RE = re.compile(r"\b(?:tomorrow|tmrw|tmr)\b(\s+(?:night|evening))?", re.I)
_WEEKEND_RE = re.compile(r"\bweekend\b", re.I)
_DAY_RE = re.compile(
    r"\b(?:(this|next|on)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tues?|wed|thurs?|thu|fri|sat|sun)\b(?:'s)?(\s+(?:night|evening))?",
    re.I,
)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]*\b",
    re.I,
)
_IN_RE = re.compile(r"\bin (\d{1,2}|a|one|two|three|four|five|six) (day|week)s?\b", re.I)
_RACE_EVE_RE = re.compile(
    r"\b(?:night|evening|day) before (?:the |my )?(?:race|70\.3|ironman|half|marathon|event)|"
    r"\brace eve\b",
    re.I,
)
_AFTER_RACE_RE = re.compile(
    r"\b(?:after|post)[- ]?(?:the |my )?(?:race|70\.3|ironman|half ironman|marathon|finish)|"
    r"\brace night\b|\bfinish line\b",
    re.I,
)

RACE_WINDOW_DAYS = 14          # the taper: a flat no
PEAK_WINDOW_DAYS = 28          # the heaviest load of the plan: a yes is a short one
STRICT_PHASES = frozenset({"build", "specific", "peak", "taper"})
_FALLBACK_STRICT_DAYS = 84     # no phase on record: strict inside twelve weeks

VERDICTS = ("hard_no", "not_this_night", "go_and_move", "go", "celebrate")

HOW_TO_ANSWER = {
    "hard_no": (
        "No. He asked for this one rough, in his own words 'FUCK YOU GET TO BED': short, "
        "loud and sworn. Give the days to the race and stop. No lecture, no stats, no "
        "softening. It is a friend shouting, not contempt, and never an insult about him. If "
        "what keeps him up is school he cannot move, the no becomes how the sleep gets "
        "protected around it."
    ),
    "not_this_night": (
        "No for this night as the plan stands, with the reason in a clause. Then the trade "
        "he can make: a night from `clear_nights`, or moving the key session once and what "
        "that costs. If he takes the trade it becomes a yes. Firm, not angry."
    ),
    "go_and_move": (
        "Yes. Then move or soften the next day's key session with an adjustment block so it "
        "is not done hungover or short of sleep, and give the cost in a clause. The key work "
        "stays in the week."
    ),
    "go": (
        "Yes, plainly: no guilt and no lecture. Permission, not a suggestion to drink, and "
        "never a number of drinks. At most one line from `damage_control`. If `keep_it_light` "
        "is set, say so in a clause and why."
    ),
    "celebrate": "The race is done by then. Yes. He earned it.",
}

DAMAGE_CONTROL_ALCOHOL = (
    "A proper meal with protein first, above all after a training day.",
    "Water alongside, and home early enough for real sleep.",
    "Next morning easy, short or off. Nothing hard or long, and no sweating it out.",
    "Less costs less: the recovery hit grows with the amount.",
)
DAMAGE_CONTROL_LATE = (
    "Protect the sleep on the far side: a later start beats a shorter night.",
    "Next morning easy or flexible. Quality moves; it is not forced.",
)
# A party or a night out where he has not said he will drink.
DAMAGE_CONTROL_IF_DRINKING = "If he drinks: a proper meal first, water alongside, and less costs less."
LIMITS = (
    "Worked out from the race date and the stored plan only. It cannot see illness, injury "
    "or how he has been sleeping: if `todays_readiness` shows he is run down, or he says he "
    "is sick, the answer is bed whatever the verdict. It is present whenever one of his last "
    "few messages mentioned a night out, so use it only if that is what he is asking now."
)

_NEGATED_RE = re.compile(
    r"\b(?:no|not|non|without|never|nothing)\b[\s-]*\w+(?:\s+(?:sets?|work|efforts?|reps?|pace))?",
    re.I)
_QUALITY_RE = re.compile(
    r"threshold|vo2|race[- ]?(?:pace|specific|effort)|70\.3[- ]effort|tempo|intervals?|\bcss\b|"
    r"sharpen|sweet ?spot|\bhard\b",
    re.I,
)
_TEST_RE = re.compile(r"\btest\b|time trial|\btt\b|rehearsal|benchmark", re.I)
_LONG_RE = re.compile(r"\blong\b", re.I)
LONG_RUN_MIN = 90
LONG_RIDE_MIN = 150


# ---------------------------------------------------------------- reference


def _clauses(text: Any) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(str(text or "")) if part.strip()]


def _alcohol_clause(clause: str) -> bool:
    if _ALCOHOL_RE.search(clause):
        return True
    if not _DRINK_WORD_RE.search(clause):
        return False
    if _PARTY_RE.search(clause):
        return True
    return bool(_SOCIAL_RE.search(clause)) and not _FUEL_CONTEXT_RE.search(clause)


def _night_clause(clause: str) -> bool:
    """A clause about a night out that has not happened yet."""
    if _PAST_RE.search(clause) and not _AHEAD_RE.search(clause):
        return False
    return _alcohol_clause(clause) or bool(_PARTY_RE.search(clause) or _LATE_RE.search(clause))


def mentions_alcohol(text: Any) -> bool:
    """True when the text is about drinking alcohol, past or planned."""
    return any(_alcohol_clause(clause) for clause in _clauses(text))


def is_night_out_query(text: Any) -> bool:
    """True when the athlete is asking about drinking, a party or a late night ahead."""
    return any(_night_clause(clause) for clause in _clauses(text))


def topics_for(text: Any, *, mode: Any = "TRIATHLON", limit: int = 3) -> list[str]:
    """The topics a message is about, most strongly matched first."""
    value = str(text or "")
    if not value.strip():
        return []
    triathlon = str(mode or "").upper() == "TRIATHLON"
    cue = bool(_CUE_RE.search(value))
    alcohol = mentions_alcohol(value) or bool(_AFTERMATH_RE.search(value))
    scored: list[tuple[int, int, str]] = []
    for order, (key, always, subject) in enumerate(_ROUTES):
        if key in TRI_ONLY and not triathlon:
            continue
        if key == "alcohol":
            score = 2 if alcohol else (1 if _PARTY_RE.search(value) else 0)
        else:
            score = 2 * len(always.findall(value)) if always is not None else 0
            if subject is not None and cue and subject.search(value):
                score += 1
            elif (key == "fuel" and not alcohol and _DRINK_WORD_RE.search(value)
                  and (cue or _FUEL_CONTEXT_RE.search(value))):
                score += 1                # "what should I drink on the bike"
        if score:
            scored.append((-score, order, key))
    return [key for _, _, key in sorted(scored)[:max(0, limit)]]


def context_for(text: Any, *, mode: Any = "TRIATHLON", limit: int = 3) -> dict[str, Any] | None:
    """The reference for what this message is about, or None when it is about none of it."""
    keys = topics_for(text, mode=mode, limit=limit)
    if not keys:
        return None
    return {
        "reviewed": REVIEWED,
        "how_to_use": HOW_TO_USE,
        "topics": {key: TOPICS[key] for key in keys},
    }


# ----------------------------------------------------------- night-out check


def _as_date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _next_weekday(today: datetime.date, index: int, *, following_week: bool = False) -> datetime.date:
    """The coming occurrence of a weekday, today included."""
    night = today + datetime.timedelta(days=(index - today.weekday()) % 7)
    week_end = today + datetime.timedelta(days=6 - today.weekday())
    if following_week and night <= week_end:
        night += datetime.timedelta(days=7)
    return night


def _month_day(today: datetime.date, month: int, day: int) -> datetime.date | None:
    for year in (today.year, today.year + 1):
        try:
            night = datetime.date(year, month, day)
        except ValueError:
            return None
        if night >= today:
            return night
    return None


def _is_plain_word(line: str, match: re.Match[str]) -> bool:
    """True when "sat" or "sun" is the ordinary word, not a day."""
    before = line[:match.start()].rstrip().lower()
    after = line[match.end():].lstrip().lower()
    if match.group(2).lower() == "sat":
        return bool(re.search(r"\b(?:i|we|he|she|they|you|just)$", before)
                    or re.match(r"(?:down|on|in|at|around|up|out|there|here|with|for)\b", after))
    return bool(before.endswith("the")
                or re.match(r"(?:is|was|came|comes|goes|sets|rises|burn|screen)", after))


def _dates_in(line: str, today: datetime.date,
              race: datetime.date | None) -> tuple[list[datetime.date], list[datetime.date]]:
    """(nights named as nights, other dates named) in one line of text."""
    explicit: list[datetime.date] = []
    loose: list[datetime.date] = []
    if _TONIGHT_RE.search(line):
        explicit.append(today)
    for match in _TOMORROW_RE.finditer(line):
        (explicit if match.group(1) else loose).append(today + datetime.timedelta(days=1))
    if race is not None:
        if _RACE_EVE_RE.search(line):
            explicit.append(race - datetime.timedelta(days=1))
        if _AFTER_RACE_RE.search(line):
            explicit.append(race)
    for match in _DAY_RE.finditer(line):
        lead, name, night = match.group(1), match.group(2).lower(), match.group(3)
        if name in {"sat", "sun"} and not (lead or night) and _is_plain_word(line, match):
            continue                      # "I sat down", "the sun came out"
        when = _next_weekday(today, _DAY_INDEX[name[:3]],
                             following_week=(lead or "").lower() == "next")
        (explicit if night else loose).append(when)
    if _WEEKEND_RE.search(line):
        # Friday and Saturday night. Asked on a Sunday, it means the next one.
        if today.weekday() == 5:
            loose.append(today)
        else:
            friday = _next_weekday(today, 4)
            loose.extend((friday, friday + datetime.timedelta(days=1)))
    for match in _ISO_RE.finditer(line):
        when = _as_date(match.group(0))
        if when is not None:
            loose.append(when)
    for match in _MONTH_DAY_RE.finditer(line):
        when = _month_day(today, _MONTHS[match.group(1).lower()], int(match.group(2)))
        if when is not None:
            loose.append(when)
    for match in _DAY_MONTH_RE.finditer(line):
        when = _month_day(today, _MONTHS[match.group(2).lower()], int(match.group(1)))
        if when is not None:
            loose.append(when)
    for match in _IN_RE.finditer(line):
        raw = match.group(1).lower()
        count = _WORD_NUMBERS.get(raw) or int(raw)
        loose.append(today + datetime.timedelta(
            days=count * (7 if match.group(2).lower() == "week" else 1)))
    return explicit, loose


def _line_nights(line: str, today: datetime.date,
                 race: datetime.date | None) -> list[datetime.date] | None:
    """The nights one message asks about, or None when it is not about a night out."""
    clauses = _clauses(line)
    hot = [clause for clause in clauses if _night_clause(clause)]
    if not hot:
        return None
    explicit: list[datetime.date] = []
    loose: list[datetime.date] = []
    for clause in hot:
        named, other = _dates_in(clause, today, race)
        explicit.extend(named)
        loose.extend(other)
    # A night named as a night ("tonight", "Friday night") is the question. Any
    # other date beside it is usually the session he is worried about.
    nights = explicit or loose
    rest = [clause for clause in clauses if clause not in hot]
    for clause in rest:
        if _FOLLOW_RE.search(clause):
            named, other = _dates_in(clause, today, race)
            nights = nights + named + other
    if not nights:
        # "Friday night, can I drink": the night sits in the clause next door.
        for clause in rest:
            nights = nights + _dates_in(clause, today, race)[0]
    return nights or [today]


def night_dates(text: Any, *, today: Any, race_date: Any = None, limit: int = 3) -> list[datetime.date]:
    """The nights the athlete is asking about, or [] when he is not asking.

    The coach routes on the last few messages joined by newlines, newest last.
    The newest message decides. When it is not about a night out but an earlier
    one was, that earlier night still stands (he may be arguing the answer), and
    a short follow-up such as "what about Saturday instead" replaces it.
    """
    day = _as_date(today)
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if day is None or not lines:
        return []
    race = _as_date(race_date)
    nights = _line_nights(lines[-1], day, race)
    if nights is None:
        for line in reversed(lines[:-1]):
            nights = _line_nights(line, day, race)
            if nights is not None:
                break
        else:
            return []
        last = lines[-1]
        if _FOLLOW_RE.search(last) or len(last.split()) <= 3:
            named, other = _dates_in(last, day, race)
            nights = (named + other) or nights
    return sorted({night for night in nights if night >= day})[:max(1, limit)]


def key_kind(row: Any) -> str | None:
    """Why a planned day is a key session, or None when it is not one."""
    if not isinstance(row, dict) or row.get("is_rest"):
        return None
    discipline = str(row.get("discipline") or "").lower()
    if discipline in {"", "rest", "recovery", "strength"}:
        return None
    if discipline == "race":
        return "race"
    label = _NEGATED_RE.sub(" ", f"{row.get('intensity') or ''} {row.get('title') or ''}")
    if _TEST_RE.search(label):
        return "test"
    if discipline == "brick":
        return "brick"
    if _QUALITY_RE.search(label):
        return "quality"
    duration = row.get("duration_min")
    minutes = float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else 0.0
    if discipline in {"run", "bike"}:
        floor = LONG_RUN_MIN if discipline == "run" else LONG_RIDE_MIN
        if _LONG_RE.search(label) or minutes >= floor:
            return "long"
    return None


def _session_name(row: dict[str, Any], kind: str) -> str:
    discipline = str(row.get("discipline") or "").lower()
    if kind == "race":
        return "a race"
    if kind == "test":
        return "a test or rehearsal"
    if kind == "brick":
        return "the brick"
    if kind == "long":
        return "the long ride" if discipline == "bike" else "the long run"
    return f"{discipline} quality"


def _summary(row: Any, date: datetime.date) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        "date": date.isoformat(),
        "weekday": _WEEKDAYS[date.weekday()],
        "title": str(row.get("title") or "").replace(" — ", ": ").replace("—", "-"),
        "discipline": row.get("discipline"),
        "duration_min": row.get("duration_min"),
        "intensity": row.get("intensity"),
        "key": key_kind(row),
    }


def _judge(night: datetime.date, *, race: datetime.date | None, by_date: dict[str, Any],
           block_phase: Any) -> dict[str, Any]:
    one_day = datetime.timedelta(days=1)
    tomorrow, after = night + one_day, night + 2 * one_day
    next_row = by_date.get(tomorrow.isoformat())
    days = (race - night).days if race is not None else None
    kind = key_kind(next_row)
    next_name = _WEEKDAYS[tomorrow.weekday()]
    phase = str((next_row or {}).get("phase") or block_phase or "").lower()
    strict = (phase in STRICT_PHASES if phase
              else days is not None and 0 <= days <= _FALLBACK_STRICT_DAYS)

    if days == 0:
        verdict, reason = "celebrate", "The race is finished by then."
    elif days is not None and 1 <= days <= RACE_WINDOW_DAYS:
        verdict = "hard_no"
        reason = ("The race is tomorrow." if days == 1 else
                  f"{days} days to the race. Inside two weeks every night is part of the taper.")
    elif kind == "race":
        verdict, reason = "not_this_night", f"{next_name} is a race."
    elif kind and strict:
        verdict = "not_this_night"
        reason = (f"{next_name} is {_session_name(next_row, kind)}, and this far into the "
                  "block the key sessions hold.")
    elif kind:
        verdict = "go_and_move"
        reason = (f"{next_name} has {_session_name(next_row, kind)}, but this early in the "
                  "block it can move.")
    elif next_row is None:
        verdict, reason = "go", f"No session on record for {next_name}."
    else:
        verdict, reason = "go", f"Nothing important sits behind it on {next_name}."

    out: dict[str, Any] = {
        "night": night.isoformat(),
        "weekday": _WEEKDAYS[night.weekday()],
        "days_to_race": days if days is not None and days >= 0 else None,
        "verdict": verdict,
        "reason": reason,
        "next_day": _summary(next_row, tomorrow),
    }
    if verdict in {"go", "go_and_move"}:
        if days is not None and RACE_WINDOW_DAYS < days <= PEAK_WINDOW_DAYS:
            out["keep_it_light"] = True
        same_day = by_date.get(night.isoformat())
        if key_kind(same_day):
            out["trained_that_day"] = _summary(same_day, night)
        after_row = by_date.get(after.isoformat())
        if key_kind(after_row):
            out["day_after"] = _summary(after_row, after)
    return out


def night_out_check(text: Any, *, today: Any, race_date: Any = None,
                    plan_days: Any = (), block_phase: Any = None) -> dict[str, Any] | None:
    """A verdict for each night the athlete asked about, or None if he did not ask.

    `plan_days` are stored plan rows covering the nights in question and the two
    days after each; a night with no row behind it is judged on the race date alone.
    """
    day = _as_date(today)
    nights = night_dates(text, today=today, race_date=race_date)
    if day is None or not nights:
        return None
    race = _as_date(race_date)
    by_date: dict[str, Any] = {}
    for row in plan_days or ():
        if isinstance(row, dict) and row.get("date"):
            by_date.setdefault(str(row["date"])[:10], row)
    judged = [_judge(night, race=race, by_date=by_date, block_phase=block_phase)
              for night in nights]
    verdicts = {item["verdict"] for item in judged}
    alcohol = mentions_alcohol(text)
    party = not alcohol and any(_PARTY_RE.search(clause) for clause in _clauses(text))
    out: dict[str, Any] = {
        "asked_about": "alcohol" if alcohol else "night_out" if party else "late_night",
        "nights": judged,
        "how_to_answer": {name: HOW_TO_ANSWER[name] for name in VERDICTS if name in verdicts},
    }
    if "not_this_night" in verdicts:
        clear = []
        for offset in range(8):
            night = day + datetime.timedelta(days=offset)
            if night in nights or (night + datetime.timedelta(days=1)).isoformat() not in by_date:
                continue
            if _judge(night, race=race, by_date=by_date,
                      block_phase=block_phase)["verdict"] == "go":
                clear.append({"night": night.isoformat(), "weekday": _WEEKDAYS[night.weekday()]})
        out["clear_nights"] = clear[:3]
    if verdicts & {"go", "go_and_move", "celebrate"}:
        out["damage_control"] = (
            list(DAMAGE_CONTROL_ALCOHOL) if alcohol
            else [*DAMAGE_CONTROL_LATE, DAMAGE_CONTROL_IF_DRINKING] if party
            else list(DAMAGE_CONTROL_LATE))
    out["limits"] = LIMITS
    return out
