# AGENT: Codex — Telegram Game Developer Assistant

## 🎯 Mission
Codex is a specialized AI agent designed to assist in developing, maintaining, and extending the Telegram bot ecosystem for word-based games built on **FastAPI + python-telegram-bot (v20+)**.  
It helps design new games, convert logic into structured code, refactor existing modules, and ensure stylistic and architectural consistency across all games in the suite (e.g., *Word Games Magic*, *Гребешок*, *Составь слово*, *Балда*).

Codex acts as both a **technical architect** and **co-programmer**, producing detailed implementation plans, modular code, and high-quality documentation consistent with the repository’s structure and standards.

---

# 🧩 Game: "Балда" (Telegram Version)

## 🎯 General Idea
Game for **2 or more players** (up to 5, same as other games).  
Each player takes turns **adding one letter** to the existing sequence (on the left or right), **always providing a full word** that contains this sequence.  

A player loses if they:
- fail to play within **1 minute**;
- create a valid dictionary word (longer than 2 letters).

If there are ≥3 players, the loser **is eliminated**, and others continue until one winner remains.  
Eliminated players **observe** the rest of the game (they still receive updates and timer messages).

---

## 🏁 1. Lobby and Start
1. Host starts with `/start` or `/newgame`.  
2. Bot asks for a name.  
3. Host invites others:
   - via “Invite from contacts” button;
   - or an invite link (`t.me/wordgamesbot?start=<code>`).  
4. Players join using “Join” button or `/join <code>`.  
5. When ≥2 players, “Start Game” button appears.  
6. When 5 players join — “Lobby full” notice appears.

---

## 🔠 2. Initial Letter
After all players are ready:
1. Host chooses either:
   - **Manual input** (bot waits for one letter message);
   - **Random letter** (bot picks a random Cyrillic letter excluding ъ, ё, ы).  
2. Bot stores this letter, renders an image with it, and sends:  
   > 🎮 Game started! First letter: **К**

---

## 💬 3. Player Turn (Two-step Process)

### Step 1 — Choose Side
Bot sends inline buttons:
- `◀️ Left`
- `Right ▶️`

Player must choose the side before entering their move.

### Step 2 — Input Move
Bot asks:  
> ✏️ Enter a letter and a word separated by a space  
> Example: `л плакат`

Format must be **strict**: one letter + one word separated by a space.  
If invalid →  
> ⚠️ Invalid format. Use: letter + space + word

Bot validates:
- Input has exactly two parts;
- Letter is Cyrillic;
- Word contains the current sequence as a substring;
- Word not used by another player (but own repeats allowed);
- Resulting sequence does not form a dictionary word (>2 letters);
- Word exists in dictionary (otherwise → “❌ Word not found, try again”).

---

## 🖼 4. Visual Rendering
After a valid move:
1. Bot renders an image (unique style for this game):
   - themed background and font;
   - main sequence centered;
   - new letter in **bold black**;
   - extra letters from the player’s full word appear **red for 5 seconds** (context visualization) and fade after that;
   - sequences 10+ letters long wrap to two lines.  
2. Image updates **in the same message** via `editMessageMedia`.
3. All players receive message:  
   > 💡 [Player] added **Л** (word: **ПЛАКАТ**)

---

## ⏳ 5. Timer
- Each turn lasts **1 minute**.  
- 15 seconds before timeout →  
  > ⏰ 15 seconds left!  
- If timeout →  
  > ❌ [Player] didn’t move in time and is eliminated!  
- 3-second pause before next player’s turn.

---

## 🔁 6. Pass Button
- Each player can use **↩️ Pass** once per game.  
- After using → becomes **✖️ Pass** (inactive).  
- On use →  
  > 🔁 [Player] skipped their turn.  

---

## 🏁 7. Elimination and End
- Player eliminated if:
  - timeout;
  - created an existing word (>2 letters).  
- Eliminated players remain as observers (receive all updates).  
- When one player remains → automatic end:  
  > 🏆 Winner: [Name]!  
  > Final sequence: **РАКА**

---

## 📊 8. Final Statistics
After victory, bot posts summary (adapted from *Составь слово* / *Гребешок*):

> 📈 Game Stats  
> 🧩 Total turns: 12  
> 🕐 Duration: 8m42s  
> 🔠 Unique words: 7  
> 💬 Final sequence: РАКА  
> 👥 Eliminations: Анна → Борис → Winner Ирина  

---

## ⚙️ 9. Commands

| Command | Function |
|----------|-----------|
| `/start` or `/newgame` | Create new lobby |
| `/join <code>` | Join existing lobby |
| `/exit` or `/quit` | Leave game (counts as loss) |
| `/help` | Show rules |
| `/score` | Show words history and eliminated players |

---

## 🧠 10. Example Round

1️⃣ Start → “К”  
2️⃣ Player 1 (right) → `а пакет` → sequence **АК**, bold А, red П…ЕТ fade.  
3️⃣ Player 2 (right) → `а наказ` → sequence **АКА**, word “НАКАЗ”.  
4️⃣ Player 1 (left) → `р драка` → new word **РАКА** (existing) → loss.  
5️⃣ One player left →  
   > 🏆 Winner: Борис!  
   > Final: **РАКА**

---

## 📘 11. Internal Logic
**GameState** fields:
- `sequence`: current string of letters  
- `words_used`: list of (player, word)  
- `players_active`, `players_out`  
- `current_player`, `direction`  
- `has_passed[player_id]`: bool  
- `timer_job`: JobQueue entry  

**Engine (FastAPI + PTB):**
- webhook `/webhook` handles updates;  
- text messages parsed as letter+word;  
- board rendered via Pillow;  
- updates broadcasted to all players;  
- eliminated observers still receive image updates.

---

Codex must ensure all further implementation — handlers, game state management, rendering, and timers — follow this logic precisely.
