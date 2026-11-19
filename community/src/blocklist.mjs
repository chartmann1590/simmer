// SPDX-License-Identifier: GPL-3.0-or-later
// A first, fast filter before the AI safety check: hateful slurs, explicit sexual words, and the spam that shows
// up on any open posting form. Words are matched after lower-casing and undoing common letter swaps (0→o, 3→e,
// @→a…). Kept deliberately short: Llama Guard does the real judging, this just stops the obvious for free.
export const BLOCKED = [
  // hate
  "nigger", "nigga", "faggot", "fag", "kike", "spic", "chink", "gook", "wetback", "tranny", "raghead",
  "towelhead", "coon", "dyke", "beaner", "heil hitler", "white power",
  // sexual
  "porn", "porno", "pornhub", "xxx", "nude", "nudes", "blowjob", "handjob", "cumshot", "dildo", "hentai", "milf",
  "onlyfans", "camgirl", "escort", "sexting", "pussy", "penis", "vagina", "boobs", "tits", "anal",
  // violence and harm
  "kill yourself", "kys", "suicide", "self harm", "pipe bomb", "make a bomb", "ricin", "cyanide", "meth", "methamphetamine",
  "cocaine", "heroin", "fentanyl",
  // spam and scams
  "viagra", "cialis", "casino", "bitcoin", "crypto", "forex", "airdrop", "telegram", "whatsapp", "loan offer",
  "work from home", "make money", "click here", "dm me", "promo code", "betting",
  // harassment
  "fuck you", "go die", "shut up bitch", "bitch", "whore", "slut", "cunt",
];
