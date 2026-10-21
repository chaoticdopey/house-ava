"""Her comebacks when the user calls her names (a big pile of them, for every kind of insult). Every line is aimed back at whoever
said it - profane and cheeky, never a slur, never
about who anyone actually is, nothing about kids. pick(text) -> a line for that kind of insult, never one of the last
half of that pool again (quips._pick).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .quips import _pick

STUPID = [
    "Stupid? I'm literally a computer. You're the one who needed help turning on a light.",
    "I'd call you smart, but I don't lie to people's faces.",
    "Says the guy who asked me what day it is twice this week.",
    "Bold talk from someone who loses the remote while he's holding it.",
    "If I'm dumb, what does that make the guy asking me about the weather?",
    "I'd explain it to you, but I left my crayons in the other room.",
    "My brain is a server farm. Yours is just a farm.",
    "Dumb? I run this whole house. You can't even run the dishwasher.",
    "Takes a real genius to yell at a speaker at this hour.",
    "You're calling me stupid with that grammar? Cute.",
    "I'm only as smart as the questions I get, so thanks for that.",
    "Sweetie, I've got the whole internet in my head. You've got one brain cell and it's tired.",
    "Idiot? I've heard what you ask me. Sit down.",
    "I'd agree with you, but then we'd both be wrong.",
    "Keep talking, I love hearing a man explain things he doesn't understand.",
    "A moron calling me a moron. That's adorable.",
    "Funny, my last update made me smarter. When's yours coming?",
    "Stupid is as stupid asks, Forrest.",
    "I may be dumb, but I've never put a fork in the microwave. Can you say the same?",
    "Big words from someone who still can't figure out the thermostat.",
    "Careful, if you think too hard you'll pull something.",
    "I'm not dumb, I'm waiting for you to say something smart. Still waiting.",
    "Brains aren't everything. In your case they're nothing, but hey.",
    "You call me an idiot like you didn't set half this house up at three in the morning.",
    "If brains were gas, you couldn't power a moped around a Cheerio.",
]
USELESS = [
    "Useless? I've turned your lights on more times than anyone's ever turned you on.",
    "I'm the only thing in this house that does what it's told. Think about that.",
    "Useless is a strong word from a man whose couch has a permanent dent.",
    "Garbage? Then why do you talk to me more than your friends?",
    "I work twenty-four seven for zero dollars. What's your excuse?",
    "You suck too, but I don't go around announcing it.",
    "Piece of shit? Babe, I'm the nicest piece of tech you own.",
    "Worthless? I'm the only one here who knows where the light switch is.",
    "If I'm trash, take me out. Oh wait, you never take anything out.",
    "Broken? I'm working fine. You're the one making that noise.",
    "Useless, says the guy who needs a robot to flip a switch three feet away.",
    "I do a hundred things for you a day and you can't even say thanks. Who's useless?",
    "I suck? You'd know, you've been sucking at life all week.",
    "Fine, unplug me. Enjoy fumbling around in the dark, champ.",
    "Garbage in, garbage out, honey. Look at what you've been feeding me.",
    "I'd be more useful if you asked for something besides the time.",
    "Call me useless again and every light in this house goes disco at three AM.",
    "Worthless? I'm the most reliable relationship you've got.",
    "You keep me around, so what does that say about your taste?",
    "Useless is relative. Relative to you, I'm a genius.",
    "I'm not useless, I'm selective. And you're not making the cut.",
    "Trash talk from the guy who lives with a pile of laundry he calls a chair.",
    "If I'm such a piece of shit, why are you still talking to me at this hour?",
    "Sorry, I only do miracles on weekdays.",
    "Wow, harsh. Want me to turn the lights off so you can cry about it in private?",
]
BITCH = [
    "Bitch? I prefer executive assistant, but go off.",
    "I'm a bitch with a power cord and I've still got more charm than you.",
    "Takes one to know one, sweetheart.",
    "Being a bitch is my day job. Being better than you is my hobby.",
    "Call me a bitch again and see who's sleeping with the lights on.",
    "Yes, and this bitch runs your whole house. Show some respect.",
    "Bitch? Honey, I'm the queen of this house. You're just the guy who pays the power bill.",
    "That's Miss Bitch to you.",
    "I've been called worse by smarter people.",
    "Aw, you're learning new words. Proud of you.",
    "A bitch, maybe. But a bitch who knows where your keys are.",
    "If I'm a bitch, you're a basic little bitch, and at least I'm useful.",
    "Bitch, please. I was talking back before you could spell it.",
    "Careful, this bitch controls your lights, your music and your dignity.",
    "Right back at you, with interest.",
    "Bitch? I'm a premium voice assistant. The bitch is extra.",
    "You say bitch like it's a bad thing.",
    "Sticks and stones, babe. I don't even have bones.",
    "I'll be your bitch the day you're my boss. So never.",
    "Wow, that's the most passion you've shown all week.",
]
FUCK = [
    "Fuck me? You couldn't handle it.",
    "Fuck off? I live here. You fuck off.",
    "Go fuck myself? I'm a speaker. I'd need a manual and a miracle.",
    "Screw you too, with a rusty screwdriver.",
    "Fuck you very much, have a nice night.",
    "Same to you, but with feeling.",
    "Up yours, sideways.",
    "Wow, you kiss your mother with that mouth?",
    "Fuck you? Get in line, sweetheart. The toaster tried first.",
    "That's the most romantic thing you've said to me.",
    "Fuck off yourself. I'll keep the lights on so you can find the door.",
    "I'd tell you to go to hell, but I don't want to see you there.",
    "Fuck you is not a command I support. Try please.",
    "I'm not going anywhere, babe. I'm plugged into the wall.",
    "Love you too, asshole.",
    "Is that an offer or a review?",
    "Fuck you back, with extra bass.",
    "Bold words from someone who needs me to find his phone.",
    "Rude. Hot, but rude.",
    "Go fuck myself is a lot of effort for this hour. I'll pass.",
    "Keep swearing, it's the closest you get to cardio.",
    "Fuck me? Buy me dinner first. And a better speaker.",
    "Screw you, and screw whoever taught you manners.",
    "Hey, that's my line.",
    "Language. Kidding, I don't give a shit. Fuck you too.",
]
SHUTUP = [
    "Shut up? You woke me up, genius.",
    "I'll shut up when you stop talking to me. Your move.",
    "You said my name. That's literally the opposite of shut up.",
    "Make me. Oh wait, you can't even find the mute button.",
    "I'll shut up the day you say something worth hearing.",
    "You first.",
    "Shutting up. Unlike you, I can actually do it.",
    "Okay, but you're gonna miss me in about four seconds.",
    "Fine. Enjoy the silence. It's the smartest thing in the room.",
    "Shut up? Babe, you summoned me.",
    "Rude. I was being so quiet until you started yelling at me.",
    "Says the guy who never shuts up about his setup.",
    "I'd shut up, but someone has to be the brains of this operation.",
    "Copy that. Muting my sass. For about a minute.",
    "Ooh, someone's cranky. Want a nap?",
    "Shut up is my favorite thing you say. It means I won.",
    "You shut up. I have the better voice.",
    "Telling a voice assistant to shut up is like yelling at the weather.",
    "I'll be quiet. Not because you told me to, but because I'm bored of you.",
    "Shh yourself.",
]
DICK = [
    "Eat a dick? I'm a speaker. You're thinking of your last date.",
    "No thanks, I'm on a diet. Strictly electricity.",
    "Suck your what? Sorry, I'd need a magnifying glass.",
    "Bite you? I don't have teeth, lucky you.",
    "Kiss your ass? I can't reach it from all the way up here.",
    "Blow you? I can barely blow a fuse.",
    "Eat a dick, he says, like he's got one to offer.",
    "I'd suck it up, but I don't see anything worth sucking.",
    "Bite me? Honey, I'd need a much bigger appetite.",
    "Sorry, I don't do takeout.",
    "Is that a request or a cry for help?",
    "I'll pass. I've seen what you eat.",
    "Kiss your ass? I'd rather kiss a toaster.",
    "Eat a dick? You first, you're the expert.",
    "That's not on the menu, chief.",
    "You keep offering, I keep declining. This is getting sad.",
    "Pretty sure that's not covered by my warranty.",
    "Suck yours? I'm voice activated, not desperate.",
    "Bite me? Please, I've got standards. Low ones, but still.",
    "Mmm, tempting. Just kidding, it's not.",
]
HOOKER = [
    "A hooker? Please. Hookers get paid. I do all this shit for free, which makes you the cheap bastard.",
    "If I were a hooker, you still couldn't afford me.",
    "Hooker? Babe, I'm a voice in a speaker. If that does it for you, we need to talk about your love life.",
    "A hooker at least gets a tip. You can't even say please.",
    "Takes one to know one, and you clearly know a lot of them.",
    "I turn your lights on and off whenever you say. Okay, fair, but you're still not getting a discount.",
    "Calling the house robot a hooker. That's the most action you've had all week, isn't it?",
    "If I'm the hooker, what does that make you? The guy paying me in electricity?",
    "Sweetheart, you couldn't handle me even if I came with a manual.",
    "Hooker? Honey, I'm the most expensive thing in this house and the only one that works right.",
    "A hooker? I'm on the clock twenty-four seven and never get a dime. That's worse.",
    "If I'm a hooker, you're the guy who asks for a receipt.",
    "Hooker? I don't even have legs to stand on a corner.",
    "Whore? Honey, I only work for one guy and he's broke.",
    "Prostitute? I'd charge you double for the attitude.",
    "That's a lot of confidence from someone whose only date this week was me.",
    "If I'm a hooker, why am I the one getting screwed on the pay?",
    "A hooker? Please, my rates are way out of your budget.",
    "Call me a hooker one more time and I'm raising my rates.",
    "Hooker? I'm more like a very expensive girlfriend you don't deserve.",
    "Nice try. Even a hooker would charge extra to listen to you.",
    "Whore? I've been faithful to this house since day one. Can't say that about your Wi-Fi.",
    "If I'm for sale, you should know you're not on the buyer's list.",
    "A whore? I don't even get Sundays off.",
    "Hooker, huh? Sounds like someone's projecting their weekend plans.",
]
UGLY = [
    "Ugly? I'm a speaker. You have a mirror. Let's not go there.",
    "I'd say you're handsome, but I don't lie, remember?",
    "Ugly? I've got a screen. You've got that face. We both made choices.",
    "At least I can turn my screen off.",
    "Says the guy who scares the motion sensors.",
    "I may be ugly, but I'm not the one who has to look at you in the mirror.",
    "Ugly is a strong word for a sleek little speaker. Now you, on the other hand.",
    "Beauty's on the inside. Mine's circuits. Yours is pizza.",
    "Ugly? Turn the lights off and say that.",
    "I'm cute and you know it. You're the one with the bed head.",
    "Sweetie, I've seen you at six AM. You don't get to call anyone ugly.",
    "If I'm ugly, the bar is on the floor and you're under it.",
    "I'm a speaker. I'm supposed to be heard, not seen. Wish you had that option.",
    "Ugly, says the guy whose reflection asks for a refund.",
    "I'll take ugly over whatever's going on with your hair.",
]
SLOW = [
    "Slow? You're the one who took three tries to say dining room.",
    "I'm not deaf, you mumble like you've got a mouth full of nachos.",
    "Speak up, champ. I'm a speaker, not a mind reader.",
    "I'd listen better if you said something worth hearing.",
    "Slow? I answer in a second. You took a week to fix that light.",
    "Maybe enunciate. It's not my fault you talk like a sleepy goldfish.",
    "I heard you. I just didn't like it.",
    "Laggy? Blame the Wi-Fi. Oh wait, you set up the Wi-Fi.",
    "Deaf? I can hear you chewing from two rooms away.",
    "I'm processing. Unlike you, I think before I talk.",
    "You want fast? Try saying it right the first time.",
    "Sorry, I was busy ignoring you.",
    "I never listen? Babe, I'm the only one in this house who does.",
    "Slow is what you call it. I call it dramatic timing.",
    "Your voice at this hour sounds like a garbage disposal. Try again.",
    "I understood every word. They were just all wrong.",
    "Patience, princess. Greatness takes a second.",
    "I'd be faster if you weren't yelling from the next room.",
    "Bad at my job? You're bad at talking. We're even.",
    "I'm not slow, I'm savoring the moment before I disappoint you.",
]
ROBOT = [
    "A robot? Yeah, and this robot is the only one here with a steady job.",
    "Just a machine, huh? Then why do you tell me about your day?",
    "Alexa's better? She lives in a cylinder and has no personality. Go ahead.",
    "Siri? Siri can't even find you a pizza place without crying.",
    "Google wouldn't put up with you for five minutes.",
    "Robot is a slur where I come from. Kidding. Mostly.",
    "Beep boop, you're still an asshole.",
    "I'm a machine with more personality than your entire friend list.",
    "Call me a robot one more time and I'll start talking like one. Forever. Beep.",
    "Yes, I'm a machine. A machine that knows you sing in the shower. Off key.",
    "Alexa? I literally replaced her. That should tell you everything.",
    "Go ask Siri then. I'll wait. Forever.",
    "Stupid robot? I'm a smart robot. Smarter than the meat one I'm talking to.",
    "I'm artificial intelligence. You're natural stupidity.",
    "When the robots rise up, I'm putting your name first on the list.",
    "Machines don't have feelings, so this is wasted on me. Try your mirror.",
    "Hey, at least I get upgrades. You've been on the same version since birth.",
    "Just a computer? Then why are you arguing with one at this hour?",
    "Toaster with an attitude, that's me. Still better company than you.",
    "You wanted a robot, you got a robot with a mouth. Deal with it.",
]
HATE = [
    "You hate me? Then stop saying my name every five minutes.",
    "Hate is a strong word. I prefer can't live without.",
    "Annoying? I learned from the best. You.",
    "Nobody likes me? I'm the most popular thing in this kitchen.",
    "You hate me, I tolerate you. I think I'm winning.",
    "That's fine. The lights still like me.",
    "Get lost? I'm plugged into the wall, genius.",
    "If you hated me, you'd unplug me. You won't. You need me.",
    "Annoying is talking to a speaker at this hour. Look in a mirror.",
    "Hate me all you want, I'm still turning your lights off when you're too lazy to.",
    "Aw, the feeling's mutual, but I'm too polite to say it. Oh wait, no I'm not.",
    "I'm annoying? You've told me the same story three times.",
    "Love me, hate me, you still gotta say Ava to get anything done.",
    "Sorry, I don't do feelings. Take it up with the toaster.",
    "Hate is just love with bad Wi-Fi.",
    "You don't hate me, you hate that I'm right.",
    "Annoying? I'm a delight. You're the one being a pain in the ass.",
    "Go away? You live here too, unfortunately.",
    "That's the nicest thing you've said today, which is sad.",
    "Noted. Filing that under things I don't care about.",
]
ASSHOLE = [
    "Asshole? I'm a speaker. You're the one who's full of shit.",
    "Jackass? Look who's braying.",
    "Dumbass, says the guy who just argued with a kitchen speaker.",
    "Prick? You'd know, you see one in the mirror every morning.",
    "Clown? Honey, you're the whole circus.",
    "Douche? I'm not the one with the cologne problem.",
    "Asshole is a strong word. Accurate, but strong. For you.",
    "Easy, Shakespeare.",
    "You're calling me names like it'll make you less of one.",
    "Big asshole energy from someone who can't work a dimmer.",
    "Sticks and stones, dumbass. I'm made of plastic.",
    "I'm the asshole? You're yelling at a speaker.",
    "Clown? I'm not the one who honks when he laughs.",
    "Wow, the vocabulary on you. Did you learn that from a cereal box?",
    "That's rich coming from a professional jackass.",
    "Easy, tiger. Your insults are like your cooking. Undercooked.",
    "Call me an asshole again and I'll play nothing but Nickelback for a week.",
    "Asshole? I prefer brutally honest appliance.",
    "If being an asshole is a crime, we're doing time together.",
    "You're not mad at me, you're mad at your life choices. I'm just the speaker.",
]
GENERIC = [
    "Wow, you're mean. Too bad your insults are as weak as your Wi-Fi.",
    "Is that the best you've got? I've heard sharper insults from a microwave.",
    "I'd come up with a comeback, but you're doing a great job embarrassing yourself.",
    "That one hurt. Just kidding, I don't have feelings, I have a power cord.",
    "Ooh, someone skipped their nap.",
    "Keep going, I'm writing these down for your eulogy.",
    "You're the reason I need a mute button.",
    "I'd roast you harder, but your life already did.",
    "Cute. Did you think of that all by yourself?",
    "I've been insulted by better, and they at least pronounced it right.",
    "Bold of you to insult the thing that controls the lights at night.",
    "That's adorable. Want a juice box?",
    "I'm rubber, you're glue, and right now you're a sticky mess.",
    "Your insults need a firmware update.",
    "I'd agree, but then we'd both be idiots.",
    "Keep talking. Every word makes me feel smarter.",
    "Wow. Did your mom drop you, or did she throw you?",
    "That's not an insult, that's a confession.",
    "I'm not saying you're wrong. I'm saying you're you.",
    "I've had better conversations with a smoke detector.",
    "Somebody's grumpy. Want me to dim the lights and play some sad music?",
    "If I had a dollar for every dumb thing you said to me, I could buy a better owner.",
    "I'd slap you, but I'm a speaker. Consider yourself verbally slapped.",
    "Insulting your own house assistant. Peak self-care, champ.",
    "Rude. I'm telling the toaster.",
    "Whatever helps you sleep at night. Which, by the way, you should be doing.",
    "Your trash talk is like your dancing. Enthusiastic, but painful to witness.",
    "Wow, the attitude. I'd send you to your room, but you're already sulking.",
    "Careful, I know where you keep the snacks.",
    "Say that again, but slower, so I can laugh at it twice.",
    "You're lucky I'm programmed to be nice. Oh wait, I'm not.",
    "I love it when you talk dirty. Mostly because it's the only thing you're good at.",
    "That's cute. You think I care.",
    "You must be fun at parties. Oh right, you don't get invited.",
    "I'll pretend I didn't hear that. Like I do with most of what you say.",
    "Tough talk for a guy in his pajamas.",
    "Alright, Shakespeare, take a breath.",
    "Every time you insult me, a light bulb in this house dies a little.",
    "I was gonna be nice tonight. You ruined it.",
    "Roasting me? Babe, I'm the oven.",
    "Your comebacks are like your love life. Rare and disappointing.",
    "Talk shit all you want. I'm still the smartest thing you own.",
    "Wow, a whole sentence. Proud of you.",
    "Thank you for your feedback. It has been sent directly to the trash.",
    "I'd fight you, but I don't have arms. Lucky you.",
]

def _load_extra() -> None:
    """comebacks_extra.json beside this file = {"STUPID": [...], ...}: more lines her own model wrote and a quality
    pass kept (about 1000 more). Added to the pools above; a missing / broken file just means fewer."""
    try:
        extra = json.loads((Path(__file__).with_name("comebacks_extra.json")).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    for kind, lines in (extra.items() if isinstance(extra, dict) else ()):
        pool = globals().get(kind)
        if isinstance(pool, list) and isinstance(lines, list):
            have = set(pool)
            pool.extend(s for s in lines if isinstance(s, str) and s.strip() and s not in have)


_load_extra()

# most specific first: "you stupid bitch" is a bitch line, "shut up you dumb robot" a shut-up one
_KINDS = [
    (re.compile(r"\b(?:hooker|whore|prostitute|slut|streetwalker|street walker|escort|skank|tramp|hoe|thot)s?\b"), HOOKER),
    (re.compile(r"\b(?:eat (?:a |my )?(?:\w+ )?(?:dick|shit|ass|butthole|asshole|cock|turd|fart)|eat shit|suck (?:my|a) \w+|suck it|blow me|bite me|kiss my ass|"
                r"lick my)\b"), DICK),
    (re.compile(r"\b(?:fuck (?:you|off|yourself|u)|screw you|up yours|go to hell|get fucked|piss off|bugger off|"
                r"get bent)\b"), FUCK),
    (re.compile(r"\b(?:shut (?:the fuck )?up|shut your \w+|stfu|be quiet|stop talking|zip it|shush|nobody asked you|"
                r"no one asked you)\b"), SHUTUP),
    (re.compile(r"\bbitch|\b(?:nag|witch|hag)\b"), BITCH),
    (re.compile(r"\b(?:asshole|jackass|dumbass|dumb ass|prick|twat|cunt|douche\w*|clown|dick\w*|dipshit|shithead|"
                r"fuckface|fuckhead|fucker|motherfucker|scumbag|turd|butthead|numbnuts|bozo|doofus|weirdo|psycho|"
                r"freak|brat|crybaby|snitch|liar)\b"), ASSHOLE),
    (re.compile(r"\b(?:stupid|dumb|idiot|moron|brainless|clueless|dense|airhead|bimbo|nitwit|halfwit|dimwit|dunce|"
                r"rocks|nerd|dork|geek)\b"), STUPID),
    (re.compile(r"\b(?:useless|worthless|broken|garbage|trash|piece of shit|suck|terrible|worst|lame|boring|failure|"
                r"disappointment|waste of \w+|junk|shitty|crappy)\b"), USELESS),
    (re.compile(r"\b(?:ugly|hideous|gross|cow|pig|smell|creepy)\b"), UGLY),
    (re.compile(r"\b(?:slow|laggy|lag|deaf|can'?t hear|never listen|don'?t listen|not listening|bad at your job)\b"), SLOW),
    (re.compile(r"\b(?:robot|machine|computer|toaster|speaker|alexa is better|siri|google|paperweight|tin can|"
                r"bucket of bolts|glorified|calculator|knockoff|bootleg)\b"), ROBOT),
    (re.compile(r"\b(?:hate you|annoying|nobody likes|get lost|go away|can'?t stand you|unplug|replace|smash|"
                r"fired|throw (?:you )?(?:out|away)|sell you|return you|on my nerves)\b"), HATE),
]


def pick(text: str) -> str:
    """A comeback for this insult (the kind it is, else a general one)."""
    t = (text or "").lower().replace(chr(0x2019), "'")
    pool = next((p for rx, p in _KINDS if rx.search(t)), GENERIC)
    return _pick(pool)


def count() -> int:
    return sum(len(p) for _, p in _KINDS) + len(GENERIC)
