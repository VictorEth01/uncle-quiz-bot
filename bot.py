# bot.py
import os
import sqlite3
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import docx
import PyPDF2
import io
import re

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
print(f"Loaded TELEGRAM_BOT_TOKEN: {TELEGRAM_BOT_TOKEN}")

DB_NAME = "apequiz.db"

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER UNIQUE NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT 0,
            current_question_index INTEGER DEFAULT 0,
            message_id_to_delete INTEGER
        )
    """)

    # Drop existing questions table to apply schema change easily during development
    # In a production environment, you would implement a proper database migration.
    cursor.execute("DROP TABLE IF EXISTS questions")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            options TEXT NOT NULL, -- Still stores the raw options string for display
            correct_answer_index INTEGER NOT NULL, -- Stores the 0-based index of the correct answer
            FOREIGN KEY (quiz_id) REFERENCES quizzes (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            UNIQUE(quiz_id, user_id),
            FOREIGN KEY (quiz_id) REFERENCES quizzes (id)
        )
    """)

    conn.commit()
    conn.close()

# --- Helpers ---
def get_quiz_by_group(group_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM quizzes WHERE group_id = ?", (group_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def create_quiz(group_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO quizzes (group_id) VALUES (?)", (group_id,))
    quiz_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return quiz_id

def add_questions_to_db(quiz_id, questions_data):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Delete existing questions for this quiz to allow re-uploading
    cursor.execute("DELETE FROM questions WHERE quiz_id = ?", (quiz_id,))
    for q in questions_data:
        cursor.execute(
            "INSERT INTO questions (quiz_id, question_text, options, correct_answer_index) VALUES (?, ?, ?, ?)",
            (quiz_id, q['question'], q['options'], q['answer_index']) # Use 'answer_index'
        )
    conn.commit()
    conn.close()

def parse_quiz_file(file_content, file_type):
    text = ""
    if file_type == 'application/pdf':
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
    elif file_type == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        doc = docx.Document(io.BytesIO(file_content))
        for para in doc.paragraphs:
            text += para.text + "\n"

    lines = [line.strip() for line in text.split("\n") if line.strip()]

    questions = []
    current_question = ""
    current_options_raw = [] # Stores original lines for display (e.g., "A. Option Text")
    current_options_clean = [] # Stores clean text for internal matching (e.g., "Option Text")
    current_answer_raw_input = None # The raw string from "ANSWER: X" or "ANSWER: Option Text"
    
    def save_question():
        nonlocal current_question, current_options_raw, current_options_clean, current_answer_raw_input
        if current_question and len(current_options_raw) >= 2:
            correct_answer_index = -1 # Default to -1 if not found

            if current_answer_raw_input:
                # 1. Try to match by letter/number (A, B, 1, 2)
                prefix_to_index = {'A':0, 'B':1, 'C':2, 'D':3, '1':0, '2':1, '3':2, '4':3}
                clean_answer_input = current_answer_raw_input.strip().upper()

                if clean_answer_input in prefix_to_index:
                    idx = prefix_to_index[clean_answer_input]
                    if idx < len(current_options_clean):
                        correct_answer_index = idx
                
                # 2. Try to match by full clean option text (case-insensitive)
                if correct_answer_index == -1: # Only if not found by prefix
                    for i, opt_clean in enumerate(current_options_clean):
                        if opt_clean.lower() == current_answer_raw_input.lower():
                            correct_answer_index = i
                            break

            questions.append({
                "question": current_question.strip(),
                "options": "\n".join(current_options_raw).strip(), # Store original lines for display
                "answer_index": correct_answer_index # Store the 0-based index of the correct answer
            })
        
        # Reset for next question
        current_question = ""
        current_options_raw = []
        current_options_clean = []
        current_answer_raw_input = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check for new question (more common question-starting words added)
        question_match = re.match(r"(?i)^(q[\.:]?\s*|what|which|who|how|where|when|find|select|identify|describe|explain|list|define|compare)\s*(.+)", line)
        if question_match and len(line.split()) > 2: # Ensure it's a substantial line
            save_question() # Save previous question before starting new one
            current_question = question_match.group(2).strip() # Extract question text after prefix
            continue

        # Check for answer line first, as it should never be part of options
        answer_match = re.match(r"(?i)^\s*ANSWER:\s*(.+)", line)
        if answer_match:
            current_answer_raw_input = answer_match.group(1).strip()
            continue # Do not process this line further as an option or continuation

        # Check for options
        option_match = re.match(r"^([A-Da-d1-4])[\)\.:]?\s*(.+)", line)
        bullet_option_match = re.match(r"^[-*•]\s*(.+)", line)

        if option_match:
            current_options_raw.append(line)
            current_options_clean.append(option_match.group(2).strip())
            continue
        elif bullet_option_match:
            current_options_raw.append(line)
            current_options_clean.append(bullet_option_match.group(1).strip())
            continue

        # If it's not a new question, not an answer, and not a new option, it's a continuation
        if not current_options_raw and current_question: # Continuation of question
            current_question += " " + line
        elif current_options_raw: # Continuation of an option
            current_options_raw[-1] += " " + line
            current_options_clean[-1] += " " + line

    save_question() # Save the last question after the loop finishes
    return questions

# Function to escape special MarkdownV2 characters
def escape_markdown_v2(text: str) -> str:
    """Escapes characters that have special meaning in MarkdownV2."""
    # List of characters that need to be escaped in MarkdownV2
    # Order matters for some characters, e.g., escaping '\' before other chars
    # that might contain '\'
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


# --- Commands ---
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != 'private':
        return

    file = update.message.document
    if file.mime_type not in [
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    ]:
        await update.message.reply_text("❌ Please upload a .pdf or .docx file.")
        return

    telegram_file = await file.get_file()
    file_bytes = await telegram_file.download_as_bytearray()

    context.user_data['quiz_file'] = file_bytes
    context.user_data['quiz_file_type'] = file.mime_type

    await update.message.reply_text(
        "📄 File received! Now, go to your group and use the `/upload_quiz` command to load these questions for that group."
    )

async def upload_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can use this command.")
        return

    user_id = update.message.from_user.id
    if 'quiz_file' not in context.user_data:
        await update.message.reply_text("Send your quiz file to me in DM first.")
        return

    file_content = context.user_data['quiz_file']
    file_type = context.user_data['quiz_file_type']

    try:
        questions_data = parse_quiz_file(file_content, file_type)
        logger.info(f"Parsed questions: {questions_data}")

        # Basic validation: ensure there are questions and at least 4 options for the first question
        if not questions_data or len(questions_data[0].get("options", "").split("\n")) < 4:
            raise ValueError("Invalid format or not enough options in the first question.")
        
    except Exception as e:
        logger.error(f"Parsing error: {e}")
        await update.message.reply_text(f"❌ Error processing file: {e}. Make sure the format is correct.")
        return

    group_id = update.message.chat_id
    quiz_id = create_quiz(group_id)
    add_questions_to_db(quiz_id, questions_data)
    context.user_data.clear()

    keyboard = [[InlineKeyboardButton("Join Quiz", callback_data=f"join_{quiz_id}")]]
    await update.message.reply_text("Quiz uploaded. Tap 'Join Quiz' to participate.", reply_markup=InlineKeyboardMarkup(keyboard))

async def start_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can start the quiz.")
        return

    group_id = update.message.chat_id
    quiz_id = get_quiz_by_group(group_id)

    if not quiz_id:
        await update.message.reply_text("No quiz uploaded yet.")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM participants WHERE quiz_id = ?", (quiz_id,))
    if cursor.fetchone()[0] == 0:
        await update.message.reply_text("No participants yet. Ask them to tap 'Join Quiz'.")
        conn.close()
        return

    cursor.execute("UPDATE quizzes SET is_active = 1, current_question_index = 0 WHERE id = ?", (quiz_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text("🚀 Starting quiz now...")

    # Schedule the first question after 3 seconds, passing group_id in data and as chat_id
    context.job_queue.run_once(send_question, 3, data={'group_id': group_id}, chat_id=group_id)

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current leaderboard during quiz"""
    group_id = update.message.chat_id
    quiz_id = get_quiz_by_group(group_id)
    
    if not quiz_id:
        await update.message.reply_text("No quiz found for this group.")
        return
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, score FROM participants WHERE quiz_id = ? ORDER BY score DESC", (quiz_id,))
    scores = cursor.fetchall()
    conn.close()
    
    if not scores:
        await update.message.reply_text("No participants yet.")
        return
    
    leaderboard = "🏆 *Current Leaderboard:*\n\n"
    for i, (username, score) in enumerate(scores):
        emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "🏅"
        # Escape username for MarkdownV2 before adding to leaderboard text
        escaped_username = escape_markdown_v2(username)
        leaderboard += f"{emoji} {i+1}\\. @{escaped_username} \\- {score} pts\n" # Escape '.' and '-'
    
    await update.message.reply_text(leaderboard, parse_mode="MarkdownV2") # Use MarkdownV2

# New help command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a message with social media links."""
    social_media_text = (
        "👋 Need help or want to connect?\n\n"
        "Here are my socials:\n"
        "🔗 [Twitter/X](https://x.com/Victor_Eth01?t=VLXK1ddn1vHl3FpypBiFNA&s)\n"
        "🔗 [Telegram](https://t.me/VictorEth01)\n\n"
        "Feel free to reach out\\!" # Escaping the exclamation mark
    )
    await update.message.reply_text(social_media_text, parse_mode="MarkdownV2", disable_web_page_preview=True)


# --- Button Handler ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    action = data[0]

    if action == "join":
        quiz_id = int(data[1])
        user_id = query.from_user.id
        username = query.from_user.username or query.from_user.first_name

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM quizzes WHERE id = ?", (quiz_id,))
        # Fetchone returns a tuple, check the first element
        quiz_active = cursor.fetchone()
        if quiz_active and quiz_active[0]: # Check if quiz_active is not None and its value is true
            await query.answer("Quiz already started.", show_alert=True)
            conn.close()
            return

        cursor.execute("SELECT id FROM participants WHERE quiz_id = ? AND user_id = ?", (quiz_id, user_id))
        if cursor.fetchone():
            await query.answer("You're already in.", show_alert=True)
        else:
            cursor.execute("INSERT INTO participants (quiz_id, user_id, username) VALUES (?, ?, ?)",
                           (quiz_id, user_id, username))
            conn.commit()
            await query.answer("✅ You've joined the quiz!", show_alert=True)
            # Escape username for MarkdownV2 before sending
            escaped_username = escape_markdown_v2(username)
            await context.bot.send_message(query.message.chat_id, f"✅ @{escaped_username} joined the quiz\\.", parse_mode="MarkdownV2") # Escape '.'

        conn.close()

    elif action == "answer":
        quiz_id, question_id, user_answer_index_str = int(data[1]), int(data[2]), data[3]
        user_answer_index = int(user_answer_index_str) # Convert to int
        user_id = query.from_user.id
        username = query.from_user.username or query.from_user.first_name
        chat_id = query.message.chat_id # Get chat_id here for chat_data access

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # Check if question was already answered correctly for this chat
        if context.chat_data.get(f'q_{question_id}_answered', False):
            await query.answer("This question has already been answered correctly!", show_alert=True)
            conn.close()
            return

        # Check if user exists in participants
        cursor.execute("SELECT id FROM participants WHERE quiz_id = ? AND user_id = ?", (quiz_id, user_id))
        if not cursor.fetchone():
            await query.answer("You need to join the quiz first!", show_alert=True)
            conn.close()
            return

        # Fetch correct_answer_index and options for display
        cursor.execute("SELECT options, correct_answer_index FROM questions WHERE id = ?", (question_id,))
        result = cursor.fetchone()
        if not result:
            await query.answer("Question not found!", show_alert=True)
            conn.close()
            return
        
        options_str, correct_answer_index = result
        option_lines = options_str.split('\n')
        
        # Debug logging
        logger.info(f"User answer index (from callback): {user_answer_index}")
        logger.info(f"Correct answer index (from DB): {correct_answer_index}")
        logger.info(f"Options string from DB: {options_str}")
        logger.info(f"Option lines: {option_lines}")
        if 0 <= correct_answer_index < len(option_lines):
            logger.info(f"Correct option text: {option_lines[correct_answer_index].strip()}")
        else:
            logger.info("Correct answer index out of bounds or not found.")

        if user_answer_index == correct_answer_index:
            context.chat_data[f'q_{question_id}_answered'] = True

            # Update score
            cursor.execute("UPDATE participants SET score = score + 1 WHERE quiz_id = ? AND user_id = ?",
                           (quiz_id, user_id))
            conn.commit()
            
            # Cancel any existing timeout jobs for this question
            current_jobs = context.job_queue.get_jobs_by_name(f'timeout_{question_id}')
            for job in current_jobs:
                job.schedule_removal()
            
            # --- Delete current question message ---
            cursor.execute("SELECT message_id_to_delete FROM quizzes WHERE id = ?", (quiz_id,))
            msg_to_delete = cursor.fetchone()[0]
            if msg_to_delete:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_to_delete)
                except Exception as e:
                    logger.warning(f"Could not delete message {msg_to_delete} after correct answer in chat {chat_id}: {e}")
            # --- End delete current question message ---

            # Escape username for MarkdownV2 before sending
            escaped_username = escape_markdown_v2(username)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ @{escaped_username} got it right\\! \\(\+1 point\\)", # Escape '!', '(', '+', ')'
                parse_mode="MarkdownV2"
            )
            
            # Removed the quick leaderboard update here as per user request
            
            # Schedule next question, passing group_id in data and as chat_id
            context.job_queue.run_once(send_question, 4, data={'group_id': chat_id}, chat_id=chat_id)
        else:
            await query.answer("❌ Wrong answer! Try again.", show_alert=True)
        
        conn.close()

# --- Quiz Flow ---
async def send_question(context: ContextTypes.DEFAULT_TYPE):
    # Get group_id from context.job.data
    group_id = context.job.data['group_id'] 
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Retrieve quiz data including current_question_index and message_id_to_delete
    cursor.execute("SELECT id, current_question_index, message_id_to_delete FROM quizzes WHERE group_id = ?", (group_id,))
    quiz_data = cursor.fetchone()
    if not quiz_data:
        conn.close()
        return

    quiz_id, q_index, prev_msg_id = quiz_data

    # --- Delete previous question message if exists ---
    if prev_msg_id:
        try:
            await context.bot.delete_message(chat_id=group_id, message_id=prev_msg_id)
        except Exception as e:
            logger.warning(f"Could not delete previous message {prev_msg_id} in chat {group_id}: {e}")
    # --- End delete previous message ---

    # Fetch the next question
    cursor.execute("SELECT id, question_text, options, correct_answer_index FROM questions WHERE quiz_id = ? LIMIT 1 OFFSET ?", (quiz_id, q_index))
    question_data = cursor.fetchone()

    if not question_data:
        await end_quiz(context.bot, group_id)
        conn.close()
        return

    question_id, text, options_str, correct_answer_index = question_data

    # Initialize the 'answered' state for this specific question in this chat
    context.chat_data[f'q_{question_id}_answered'] = False
    
    # Create buttons using the original option lines for display
    option_lines = options_str.split('\n')
    buttons = []
    for i, opt_display_text in enumerate(option_lines):
        # Callback data will now send the 0-based index of the selected option
        buttons.append(InlineKeyboardButton(opt_display_text, callback_data=f"answer_{quiz_id}_{question_id}_{i}"))
    
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] # Arrange buttons in rows of 2

    # Escape the question text for MarkdownV2 before sending
    escaped_question_text = escape_markdown_v2(text)

    # Send the new question message
    msg = await context.bot.send_message(
        group_id,
        f"**Q{q_index + 1}:**\n\n{escaped_question_text}", # Use escaped text here
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2" # Use MarkdownV2 for question text as well
    )

    # Update quiz state with the new current question index and message ID
    cursor.execute(
        "UPDATE quizzes SET current_question_index = current_question_index + 1, message_id_to_delete = ? WHERE id = ?",
        (msg.message_id, quiz_id)
    )

    # Schedule the timeout for this question
    context.job_queue.run_once(
        skip_question_if_unanswered,
        15, # 15 seconds timeout
        data={
            'group_id': group_id, # Pass group_id in data
            'quiz_id': quiz_id,
            'question_id': question_id
        },
        chat_id=group_id, # Pass chat_id to ensure context.chat_data is available
        name=f'timeout_{question_id}' # Unique name for this question's timeout job
    )

    conn.commit()
    conn.close()

# New function to delete a message after a delay
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data['chat_id']
    message_id = context.job.data['message_id']
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Successfully deleted message {message_id} in chat {chat_id}")
    except Exception as e:
        logger.warning(f"Could not delete message {message_id} in chat {chat_id}: {e}")


async def skip_question_if_unanswered(context: ContextTypes.DEFAULT_TYPE):
    group_id = context.job.data['group_id'] 
    question_id = context.job.data['question_id']
    quiz_id = context.job.data['quiz_id']

    # Check if question was already answered correctly for this chat
    if context.chat_data.get(f'q_{question_id}_answered', False):
        logger.info(f"Question {question_id} in group {group_id} was already answered. Skipping timeout action.")
        return  # Question was already answered correctly, do nothing

    logger.info(f"Timeout for question {question_id} in group {group_id}. Question was unanswered.")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # --- Delete the current question message ---
    cursor.execute("SELECT message_id_to_delete FROM quizzes WHERE id = ?", (quiz_id,))
    msg_to_delete = cursor.fetchone()[0]
    if msg_to_delete:
        try:
            await context.bot.delete_message(chat_id=group_id, message_id=msg_to_delete)
        except Exception as e:
            logger.warning(f"Could not delete skipped message {msg_to_delete} in chat {group_id}: {e}")
    # --- End delete current question message ---

    # Fetch correct answer text for display
    cursor.execute("SELECT options, correct_answer_index FROM questions WHERE id = ?", (question_id,))
    result = cursor.fetchone()
    conn.close()
    
    correct_answer_text = "N/A \\(Answer not found or invalid format\\)" # Default escaped
    if result:
        options_str, correct_answer_index = result
        option_lines = options_str.split('\n')
        if 0 <= correct_answer_index < len(option_lines):
            correct_answer_text = escape_markdown_v2(option_lines[correct_answer_index].strip())

    # Send the "Time's up!" message
    times_up_msg = await context.bot.send_message(
        group_id, 
        f"⏱ Time's up\\! The correct answer was: **{correct_answer_text}**\n\nMoving to the next question\\.\\.\\.", # Escape '!', '...'
        parse_mode="MarkdownV2"
    )

    # Schedule this "Time's up!" message for deletion after 60 seconds
    context.job_queue.run_once(
        delete_message_job,
        60, # Delete after 60 seconds
        data={'chat_id': group_id, 'message_id': times_up_msg.message_id},
        name=f'delete_times_up_{times_up_msg.message_id}'
    )

    # Schedule next question
    context.job_queue.run_once(send_question, 3, data={'group_id': group_id}, chat_id=group_id)


async def end_quiz(bot: Bot, group_id: int):
    quiz_id = get_quiz_by_group(group_id)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, score FROM participants WHERE quiz_id = ? ORDER BY score DESC", (quiz_id,))
    scores = cursor.fetchall()
    
    # --- Delete the last question message if it still exists ---
    cursor.execute("SELECT message_id_to_delete FROM quizzes WHERE id = ?", (quiz_id,))
    last_msg_to_delete = cursor.fetchone()
    if last_msg_to_delete and last_msg_to_delete[0]:
        try:
            await bot.delete_message(chat_id=group_id, message_id=last_msg_to_delete[0])
        except Exception as e:
            logger.warning(f"Could not delete last quiz message {last_msg_to_delete[0]} in chat {group_id} at quiz end: {e}")
    # --- End delete last question message ---

    if scores:
        leaderboard = "🏆 *Final Results:*\n\n"
        for i, (username, score) in enumerate(scores):
            emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "🏅"
            # Escape username for MarkdownV2 before adding to leaderboard text
            escaped_username = escape_markdown_v2(username)
            leaderboard += f"{emoji} {i+1}\\. @{escaped_username} \\- {score} pts\n" # Escape '.' and '-'
        
        # Add congratulations for winner
        if scores[0][1] > 0:
            # Escape winner's username for MarkdownV2
            winner_username = escape_markdown_v2(scores[0][0])
            leaderboard += f"\n🎉 Congratulations to @{winner_username} for winning\\!" # Escape '!'
        
        await bot.send_message(group_id, leaderboard, parse_mode="MarkdownV2") # Use MarkdownV2
    else:
        await bot.send_message(group_id, "🏆 Quiz completed\\! No participants scored any points\\.", parse_mode="MarkdownV2") # Escape '!'

    # Clean up database
    cursor.execute("DELETE FROM questions WHERE quiz_id = ?", (quiz_id,))
    cursor.execute("DELETE FROM participants WHERE quiz_id = ?", (quiz_id,))
    cursor.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
    conn.commit()
    conn.close()

# --- Utilities ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == 'private':
        return True
    admins = await context.bot.get_chat_administrators(update.message.chat_id)
    return update.message.from_user.id in [a.user.id for a in admins]

async def post_init(application: Application):
    await application.bot.set_my_commands([
        ("start", "Welcome message"),
        ("upload_quiz", "Admin: Upload the quiz file"),
        ("start_quiz", "Admin: Start the quiz"),
        ("leaderboard", "Show current leaderboard"),
        ("help", "Get help and social media links") # Added help command
    ])

# --- Main ---
def main():
    init_db()
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not TELEGRAM_BOT_TOKEN:
        print("❌ Please set your bot token in the TELEGRAM_BOT_TOKEN environment variable.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Yo Anon, DM me your quiz file.")))
    application.add_handler(MessageHandler(filters.Document.ALL, file_handler))
    application.add_handler(CommandHandler("upload_quiz", upload_quiz_command))
    application.add_handler(CommandHandler("start_quiz", start_quiz_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("help", help_command)) # Registered help command
    application.add_handler(CallbackQueryHandler(button_handler))
    application.run_polling()

if __name__ == "__main__":
    main()
