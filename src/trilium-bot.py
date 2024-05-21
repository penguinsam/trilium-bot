import json
import os
import sys
import threading
import validators
import re
import time
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
import urllib3, cloudscraper
from bs4 import BeautifulSoup

import schedule
import telebot
from loguru import logger
from telebot import apihelper
from telebot import types
from trilium_py.client import ETAPI

from settings import http_proxy, token, admin_list, trilium_server_url, etapi_token

apihelper.proxy = {'https': http_proxy}

bot = telebot.TeleBot(token)

data_dict = {}


@dataclass
class TODO:
    index: int = None
    description: str = None


def restricted(func):
    """Access Limit"""

    @wraps(func)
    def wrapped(message, *args, **kwargs):
        # user_id = message.chat.id
        user_id = message.from_user.id
        # Filter user id
        if user_id not in admin_list:
            logger.error(f'Access Denied for user_id: {user_id}')
            return
        return func(message, *args, **kwargs)

    return wrapped


def build_todo_list_markup(todo_list, callback_type='TODO_toggle'):
    """
    build markup for todo list
    :param todo_list:
    :param callback_type:
    :return:
    """
    markup = types.InlineKeyboardMarkup()
    for i, (status, todo) in enumerate(todo_list):
        if status:
            todo_message = '✅ ' + todo
        else:
            todo_message = '🟩 ' + todo
        markup.add(types.InlineKeyboardButton(text=todo_message,
                                              callback_data=json.dumps({
                                                  'type': callback_type,
                                                  'index': i,
                                                  'status': status
                                              })))
    return markup


def build_confirm_markup(callback_type):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(text="Yes",
                                          callback_data=json.dumps({
                                              'type': callback_type,
                                              'confirm': True
                                          })))
    markup.add(types.InlineKeyboardButton(text="No",
                                          callback_data=json.dumps({
                                              'type': callback_type,
                                              'confirm': False
                                          })))
    return markup


@bot.message_handler(commands=['id'])
def send_user_id(message):
    """
    return telegram user id
    this function has no access limit
    :param message:
    :return:
    """
    bot.reply_to(message, message.from_user.id)


@bot.message_handler(commands=['start', 'help'])
@restricted
def send_welcome(message):
    # Note: if buttons modified, a new `/start` is required to get the latest buttons.
    chat_id = message.chat.id

    markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True)

    btn_todo = types.KeyboardButton('TODO List')
    btn_toggle_quick_add = types.KeyboardButton('Toggle Quick Add')
    btn_restart = types.KeyboardButton('Restart')
    btn_status = types.KeyboardButton('Status')
    btn_id = types.KeyboardButton('ID')
    btn_add_todo = types.KeyboardButton('Add TODO')
    btn_update_todo = types.KeyboardButton('Update TODO')
    btn_delete_todo = types.KeyboardButton('Delete TODO')
    markup.row(btn_todo, btn_toggle_quick_add)
    markup.row(btn_add_todo, btn_update_todo, btn_delete_todo)
    markup.row(btn_status, btn_id, btn_restart)

    bot.set_my_commands(
        commands=[
            telebot.types.BotCommand("start", "reload function buttons"),
            telebot.types.BotCommand("move", "move yesterday's unfinished todo to today"),
        ],
        # limit only for current user
        scope=telebot.types.BotCommandScopeChat(chat_id)
    )

    bot.reply_to(message, "Hi, please choose ~", reply_markup=markup)


@bot.message_handler(commands=['move'])
@restricted
def send_welcome(message):
    """Manually move todo"""
    move_todo_job()
    bot.reply_to(message, "Move finished")


@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    logger.info(f'callback {call.data}')
    data = json.loads(call.data)
    chat_id = call.message.chat.id
    message_id = call.message.id

    if data['type'] == 'TODO_toggle':
        status = data['status']
        ea.todo_check(data['index'], check=not status)
        todo_list = ea.get_todo()
        bot.delete_message(chat_id, message_id)
        bot.send_message(chat_id, text="Current TODO List",
                         reply_markup=build_todo_list_markup(todo_list))
        return

    elif data['type'] == 'Update TODO':
        logger.info('Update todo')
        todo = TODO()
        todo.index = data['index']
        data_dict[f'{chat_id}_TODO'] = todo
        bot.delete_message(chat_id, message_id)
        msg = bot.send_message(chat_id, text="Send me new TODO description")
        bot.register_next_step_handler(msg, process_update_todo)
        return

    elif data['type'] == 'Delete TODO':
        logger.info('Delete todo')
        todo = TODO()
        todo.index = data['index']
        data_dict[f'{chat_id}_TODO_delete'] = todo
        bot.delete_message(chat_id, message_id)
        todo_description = ea.get_todo()[todo.index][1]
        bot.send_message(chat_id, text=f"Are you sure to delete '{todo_description}'",
                         reply_markup=build_confirm_markup("Delete TODO confirm"))
        return
    elif data['type'] == 'Delete TODO confirm':
        logger.info('Delete todo confirm')
        confirm = data['confirm']
        todo = data_dict[f'{chat_id}_TODO_delete']
        ea.delete_todo(todo.index)
        todo_list = ea.get_todo()
        bot.delete_message(chat_id, message_id)
        bot.send_message(chat_id, text="Current TODO List",
                         reply_markup=build_todo_list_markup(todo_list))
        return

    bot.answer_callback_query(call.id, "ok")
    return


def process_add_todo(message):
    logger.info(f'process_add_todo')
    chat_id = message.chat.id
    todo_description = message.text.strip()
    ea.add_todo(todo_description)
    todo_list = ea.get_todo()
    bot.send_message(chat_id, text="Current TODO List",
                     reply_markup=build_todo_list_markup(todo_list))


def process_update_todo(message):
    logger.info(f'process_update_todo')
    chat_id = message.chat.id
    todo = data_dict[f'{chat_id}_TODO']
    todo.description = message.text.strip()
    ea.update_todo(todo.index, todo.description)
    todo_list = ea.get_todo()
    bot.send_message(chat_id, text="Current TODO List",
                     reply_markup=build_todo_list_markup(todo_list))


def get_url_title(url):
    try:
        scraper = cloudscraper.create_scraper()
        r = scraper.get(url)
        soup = BeautifulSoup(r.text, 'html.parser')
        
        title = soup.find('title').string if r.status_code == 200 and soup.find('title') else url
        return title
    except Exception as e:
        return url


def get_url_preview(url):
    try:
        scraper = cloudscraper.create_scraper()
        r = scraper.get(url)
        soup = BeautifulSoup(r.text, 'html.parser')

        meta_image = soup.find('meta', property='og:image')
        image = meta_image['content'] if meta_image and 'content' in meta_image.attrs else None
        return f'<figure class="image image-style-block-align-left"><img src="{image}"></figure>' if image != None else None
    except Exception as e:
        return url, None


def massage_text_message(message):
    url_pattern = re.compile(r'(https?://\S+)')

    def replace_with_anchor(match):
        url = match.group(0)
        title = get_url_title(url)
        return f'<a href="{url}">{title}</a>'

    url_preview = None
    urls = url_pattern.findall(message)
    for url in urls:
        if url_preview is None:
            url_preview = get_url_preview(url)
        else:
            break

    result = url_pattern.sub(replace_with_anchor, message).replace("\n", "<br/>")
    if url_preview != None:
        return f'{result}{url_preview}'
    else:
        return result


def create_attribute_from_tags(noteId, message):
    pattern = r'#(\w+)'

    tags = re.findall(pattern, message)
    for tag in tags:
        ea.create_attribute(
            noteId=noteId,
            type='label',
            name='tag',
            value=tag,
            isInheritable=True
        )


@bot.message_handler(content_types=['text'])
@restricted
def process_text_message(message):
    logger.info(f'Receive {message.content_type} message')

    msg = message.text

    if msg in ['id', 'ID']:
        return send_user_id(message)
    elif msg in ['Toggle Quick Add', ]:
        config['quick_add'] = not config['quick_add']
        save_config()
        return bot.reply_to(message, f"quick_add {config['quick_add']}")
    elif msg in ['Restart']:
        bot.reply_to(message, f"rebooting...")
        os.execv(sys.executable, ['python'] + sys.argv)
        return
    elif msg in ['Status', ]:
        return bot.reply_to(message, f"Started {str(datetime.now() - begin_time).split('.')[0]}")
    elif msg in ['TODO List', ]:
        todo_list = ea.get_todo()
        return bot.reply_to(message, "Current TODO List", reply_markup=build_todo_list_markup(todo_list))
    elif msg in ['Add TODO', ]:
        tmp_msg = bot.reply_to(message, "Send me TODO description")
        bot.register_next_step_handler(tmp_msg, process_add_todo)
        return
    elif msg in ['Update TODO', ]:
        todo_list = ea.get_todo()
        return bot.reply_to(message, "Choose TODO item to modify",
                            reply_markup=build_todo_list_markup(todo_list, "Update TODO"))
    elif msg in ['Delete TODO', ]:
        todo_list = ea.get_todo()
        return bot.reply_to(message, "Choose TODO item to ~DELETE~",
                            reply_markup=build_todo_list_markup(todo_list, "Delete TODO"))

    if config['quick_add']:
        day_note = ea.inbox(datetime.now().strftime("%Y-%m-%d"))
        note_title = datetime.now().strftime("%Y-%m-%d %a %H:%M:%S").upper()
        logger.info(f"day_note {day_note['noteId']}")

        res = ea.create_note(
            parentNoteId=day_note['noteId'],
            title=note_title,
            type="text",
            content=massage_text_message(msg)
        )
        create_attribute_from_tags(res['note']['noteId'], msg)

        return bot.reply_to(message, f"Added to Trilium [{res['note']['noteId']}]")

    return bot.reply_to(message, msg)


@bot.message_handler(content_types=['photo'])
@restricted
def process_photo_message(message):
    day_note = ea.inbox(datetime.now().strftime("%Y-%m-%d"))
    note_title = datetime.now().strftime("%Y-%m-%d %a %H:%M:%S").upper()
    logger.info(f"day_note {day_note['noteId']}")
        
    file_id = message.photo[-1].file_id
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    photo_path = f"/tmp/{file_id}.jpg"
    with open(photo_path, 'wb') as new_file:
        new_file.write(downloaded_file)
            
    if message.caption:
        ## 1. Create a note with photo caption
        res = ea.create_note(
            parentNoteId=day_note['noteId'],
            title=note_title,
            type="text",
            content=message.caption
        )

        ## 2. Attach photo to the note
        atm = ea.create_attachment(
            ownerId=res['note']['noteId'],
            file_path=photo_path,
        )

        attachmentId = atm['attachmentId']
        role = atm['role']
        file_name = atm['title']
        ea.update_note_content(
            res['note']['noteId'], f'{message.caption}<figure class="image image-style-block-align-left"><img src="api/attachments/{attachmentId}/{role}/{file_name}"></figure>'
        )

        ## 3. Create attributes from tags, if any
        create_attribute_from_tags(res['note']['noteId'], message.caption)
    else:
        res = ea.create_image_note(
            parentNoteId=day_note['noteId'],
            title=note_title,
            image_file=photo_path,
        )
    return bot.reply_to(message, f"Added to Trilium [{res['note']['noteId']}]")


def save_config():
    global config
    global config_file
    logger.info(f'save_config')
    with open(config_file, 'w', encoding='utf-8') as f:
        f.write(json.dumps(config, ensure_ascii=False, indent=4))


def load_config():
    global config
    global config_file
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.loads(f.read())
    # default configs
    default_config = {
        'quick_add': True,
        'single_note': False,
        'move_yesterday_unfinished_todo': True,
        'move_todo_time': '00:05',
    }
    for x in default_config:
        if x not in config:
            config[x] = default_config[x]


def move_todo_job():
    logger.info("move yesterday's unfinished todo to today")
    ea.move_yesterday_unfinished_todo_to_today()


def run_bot():
    logger.info('run_bot started')
    while True:
        logger.info('start polling')
        try:
            bot.polling(none_stop=True, timeout=10)
        except Exception as e:
            logger.error(f'bot exception! {e}')
            time.sleep(1)


def run_scheduler():
    logger.info('run_scheduler started')
    if config['move_yesterday_unfinished_todo']:
        move_time = config['move_todo_time']
        schedule.every().day.at(move_time).do(move_todo_job)
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == '__main__':
    begin_time = datetime.now()
    config_file = 'config.json'
    config = {}
    load_config()
    save_config()

    ea = ETAPI(trilium_server_url, etapi_token)
    logger.info(f'{bot.get_me().username} started')
    t_bot = threading.Thread(target=run_bot)
    t_scheduler = threading.Thread(target=run_scheduler)
    t_bot.start()
    t_scheduler.start()
