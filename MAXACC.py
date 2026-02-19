from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel, PeerUser, InputPhoneContact
from telethon.tl.functions.contacts import ImportContactsRequest
import asyncio
import re
import logging
from datetime import datetime, timedelta
import secrets
from typing import Dict, List, Optional
import aiohttp
import hashlib
import time
import json
import os

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация (ОБЯЗАТЕЛЬНО ЗАМЕНИТЬ!)
API_ID = 123456  # ЗАМЕНИ НА СВОЙ API ID
API_HASH = 'ваш_api_hash'  # ЗАМЕНИ НА СВОЙ API HASH
PHONE_NUMBER = '+79991234567'  # ТВОЙ НОМЕР ТЕЛЕФОНА

# ID групп (ЗАМЕНИ НА СВОИ!)
GROUP1_ID = -1003603786886  # Группа где дропы кидают номера
GROUP2_IDS = [-1003528200513]  # Рабочие группы
ADMIN_IDS = [7876457484, 7664673617]  # ТВОЙ ID и других админов

# Топики для отчетов в группе 2 (group_id: report_topic_id)
GROUP2_REPORT_TOPICS = {
    -1003528200513: 2  # Группа: ID топика для отчетов
    # Добавь другие группы здесь: ID_ГРУППЫ: ID_ТОПИКА
}

# Топики в группе 1
GROUP1_TOPIC_ID = 1  # Основной топик для работы
GROUP1_PAYMENT_TOPIC_ID = 2  # Топик для выплат

# Crypto Pay API
CRYPTOPAY_TOKEN = "507893:AA0aFxEJlwTQrHRv6S3Tg9cJAn7LH6xmgLC"

# Настройки по умолчанию
DEFAULT_PRICE = 3.5
DEFAULT_PAYMENT_TIME = 2  # Минимальное время для выплаты (минуты)
MAX_QUEUE_SIZE = 5  # Максимальный размер очереди номеров

class CryptoPay:
    """Класс для работы с Crypto Pay API"""
    
    def __init__(self, token: str):
        self.token = token
        self.base_url = "https://pay.crypt.bot/api"
        self.headers = {
            "Crypto-Pay-API-Token": token
        }
    
    async def get_balance(self) -> Optional[float]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/getBalance", headers=self.headers) as response:
                    data = await response.json()
                    if data.get('ok'):
                        balances = data['result']
                        for balance in balances:
                            if balance.get('currency_code') == 'USDT':
                                return float(balance.get('available', 0))
                    return None
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return None
    
    async def create_invoice(self, amount: float, description: str = "") -> Optional[Dict]:
        try:
            params = {
                "asset": "USDT",
                "amount": str(amount),
                "description": description or "Пополнение баланса",
                "hidden_message": "Оплата за пополнение баланса",
                "paid_btn_name": "viewItem",
                "paid_btn_url": "https://t.me/your_bot",
                "payload": hashlib.md5(str(time.time()).encode()).hexdigest()[:16],
                "allow_comments": True,
                "allow_anonymous": False,
                "expires_in": 3600
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/createInvoice", headers=self.headers, json=params) as response:
                    data = await response.json()
                    if data.get('ok'):
                        return data['result']
                    return None
        except Exception as e:
            logger.error(f"Ошибка создания инвойса: {e}")
            return None
    
    async def check_invoice(self, invoice_id: int) -> Optional[str]:
        try:
            params = {"invoice_ids": [invoice_id]}
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/getInvoices", headers=self.headers, json=params) as response:
                    data = await response.json()
                    if data.get('ok') and data['result']['items']:
                        return data['result']['items'][0]['status']
                    return None
        except Exception as e:
            logger.error(f"Ошибка проверки инвойса: {e}")
            return None
    
    async def create_check(self, user_id: int, amount: float) -> Optional[Dict]:
        try:
            params = {
                "asset": "USDT",
                "amount": str(amount),
                "user_id": user_id,
                "pin_to_user_id": user_id,
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/createCheck", headers=self.headers, json=params) as response:
                    data = await response.json()
                    if data.get('ok'):
                        return data['result']
                    else:
                        logger.error(f"Ошибка создания чека: {data}")
                        return None
        except Exception as e:
            logger.error(f"Ошибка создания чека: {e}")
            return None

class AccountBot:
    """Бот на аккаунте пользователя через Telethon"""
    
    def __init__(self):
        self.client = TelegramClient('session_name', API_ID, API_HASH)
        self.crypto_pay = CryptoPay(CRYPTOPAY_TOKEN)
        
        # Состояния
        self.current_work = {
            'phone': None,
            'sender_id': None,
            'sender_username': None,
            'status': 'waiting_number',
            'start_time': None,
            'code_sent_time': None,
            'price': DEFAULT_PRICE,
            'is_repeat': False  # Флаг повтора
        }
        
        # Очереди
        self.trigger_queue = []
        self.number_queue = []
        
        # Активные номера
        self.active_numbers = {}  # ключ: (chat_id, topic_id)
        
        # Отчеты
        self.reports = []
        self.balance = 0.0
        
        # Настройки
        self.price = DEFAULT_PRICE
        self.payment_time = DEFAULT_PAYMENT_TIME  # Минимальное время для выплаты (минуты)
        self.work_active = True
        
        # Таймеры
        self.last_number_request = None
        self.pending_invoices = {}
        
        # Для админ-команд
        self.waiting_admin_command = None
        
        # Сообщения отчетов в группах 2
        self.report_messages = {}  # (chat_id, report_topic_id): message_id
        
        # Добавленные контакты
        self.added_contacts = set()
        
        # Статистика по группам 2
        self.group2_stats = {}  # (chat_id, topic_id): {'total': X, 'success': Y}
        
        logger.info("Бот на аккаунте инициализирован")
    
    async def start(self):
        """Запуск бота"""
        await self.client.start(phone=PHONE_NUMBER)
        me = await self.client.get_me()
        logger.info(f"Авторизован как: {me.first_name} (@{me.username}) ID: {me.id}")
        
        self.register_handlers()
        
        asyncio.create_task(self.work_cycle())
        asyncio.create_task(self.invoice_checker_cycle())
        
        await self.update_balance()
        
        logger.info("Бот запущен и готов к работе!")
        await self.client.run_until_disconnected()
    
    async def work_cycle(self):
        """Основной рабочий цикл"""
        while True:
            try:
                if not self.work_active:
                    await asyncio.sleep(5)
                    continue
                
                # Проверяем очередь номеров - если больше MAX_QUEUE_SIZE, не запрашиваем новые
                if len(self.number_queue) < MAX_QUEUE_SIZE:
                    if not self.current_work['phone'] and self.current_work['status'] == 'waiting_number':
                        current_time = datetime.now()
                        if not self.last_number_request or (current_time - self.last_number_request).total_seconds() > 120:
                            await self.request_number()
                            self.last_number_request = current_time
                
                if self.current_work['code_sent_time']:
                    time_passed = (datetime.now() - self.current_work['code_sent_time']).total_seconds()
                    if time_passed > 120:
                        await self.handle_code_timeout()
                
                await self.process_number_queue()
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"Ошибка в рабочем цикле: {e}")
                await asyncio.sleep(30)
    
    async def invoice_checker_cycle(self):
        """Цикл проверки инвойсов"""
        while True:
            try:
                await self.check_pending_invoices()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Ошибка в проверке инвойсов: {e}")
                await asyncio.sleep(60)
    
    def register_handlers(self):
        """Регистрация всех обработчиков"""
        
        # Обработчик сообщений в группе 1 (номера)
        @self.client.on(events.NewMessage(chats=GROUP1_ID))
        async def handler_group1(event):
            if event.sender_id == (await self.client.get_me()).id:
                return
            
            text = event.message.text or ''
            
            # Проверка на "повтор"
            if 'повтор' in text.lower():
                await self.handle_repeat_request(event)
                return
            
            phone_match = re.search(r'(?:\+7|7|8)\d{10}', text)
            
            if phone_match:
                await self.handle_phone_group1(event, phone_match.group())
        
        # Обработчик сообщений в группах 2
        @self.client.on(events.NewMessage(chats=GROUP2_IDS))
        async def handler_group2(event):
            if event.sender_id == (await self.client.get_me()).id:
                return
            
            text = (event.message.text or '').strip()
            if not text:
                return
            
            await self.handle_group2_message(event)
        
        # Обработчик личных сообщений
        @self.client.on(events.NewMessage(func=lambda e: e.is_private))
        async def handler_private(event):
            text = (event.message.text or '').strip()
            if not text:
                return
            
            user_id = event.sender_id
            
            # Команда /start
            if text.startswith('/start'):
                await self.cmd_start(event)
            
            # Команда /adm
            elif text.startswith('/adm'):
                await self.cmd_admin(event)
            
            # Команда /otchet
            elif text.startswith('/otchet'):
                await self.cmd_otchet(event)
            
            # Команда /deposit
            elif text.startswith('/deposit'):
                if user_id in ADMIN_IDS:
                    await self.handle_deposit_command(event, text)
                else:
                    await event.reply("🚫 Доступ запрещен")
            
            # Команда /price
            elif text.startswith('/price'):
                if user_id in ADMIN_IDS:
                    await self.handle_price_command(event, text)
                else:
                    await event.reply("🚫 Доступ запрещен")
            
            # Команда /time - изменение времени выплаты
            elif text.startswith('/time'):
                if user_id in ADMIN_IDS:
                    await self.handle_time_command(event, text)
                else:
                    await event.reply("🚫 Доступ запрещен")
            
            # Команда /reset - перезагрузка очередей
            elif text == '/reset':
                if user_id in ADMIN_IDS:
                    await self.handle_reset_command(event)
                else:
                    await event.reply("🚫 Доступ запрещен")
            
            # Команда /stop
            elif text == '/stop':
                if user_id in ADMIN_IDS:
                    self.work_active = False
                    await event.reply("⏸️ Работа остановлена")
                else:
                    await event.reply("🚫 Доступ запрещен")
            
            # Команда /startwork
            elif text == '/startwork':
                if user_id in ADMIN_IDS:
                    self.work_active = True
                    await event.reply("▶️ Работа возобновлена")
                    if not self.current_work['phone']:
                        await self.request_number()
                else:
                    await event.reply("🚫 Доступ запрещен")
            
            # Другие команды в избранном
            elif event.chat_id == await self.client.get_peer_id('me'):
                if text == '/report':
                    await self.cmd_report(event)
                elif text == '/stats':
                    await self.cmd_stats(event)
                elif text == '/balance':
                    await self.cmd_balance(event)
                elif text == '/help':
                    await self.cmd_help(event)
                elif text == '/active':
                    await self.cmd_active(event)
                elif text == '/queue':
                    await self.cmd_queue(event)
            
            # Коды и пароли от пользователей
            else:
                await self.handle_private_message(event)
    
    async def handle_repeat_request(self, event):
        """Обработка запроса повтора в группе 1"""
        sender_id = event.sender_id
        active_key = None
        phone = None
        
        for key, data in self.active_numbers.items():
            if data.get('sender_id') == sender_id:
                active_key = key
                phone = data['phone']
                break
        
        if not phone:
            await event.reply("❌ У тебя нет активных номеров для повтора")
            return
        
        # Отправляем номер снова в группу 2 (в тот же топик)
        chat_id, topic_id = active_key
        trigger = {
            'chat_id': chat_id,
            'topic_id': topic_id,
            'user_id': sender_id,
            'username': f"@{event.sender.username}" if event.sender.username else f"ID:{sender_id}",
            'timestamp': datetime.now(),
            'is_repeat': True
        }
        
        self.trigger_queue.insert(0, trigger)  # В начало очереди
        
        await event.reply(f"🔄 Отправляю номер {phone} на повтор в группу")
        
        if self.current_work['status'] == 'has_number':
            await self.process_trigger_queue()
    
    async def handle_deposit_command(self, event, text: str):
        """Обработка команды /deposit"""
        try:
            parts = text.split()
            if len(parts) != 2:
                await event.reply("❌ Используйте: /deposit 100")
                return
            
            amount = float(parts[1])
            if amount <= 0:
                await event.reply("❌ Сумма должна быть больше 0")
                return
            
            logger.info(f"Создаю инвойс на ${amount}")
            
            invoice = await self.crypto_pay.create_invoice(amount, f"Пополнение баланса на ${amount}")
            
            if invoice:
                self.pending_invoices[invoice['invoice_id']] = {
                    'amount': amount,
                    'admin_id': event.sender_id,
                    'created': datetime.now()
                }
                
                response = f"""💳 СЧЕТ НА ОПЛАТУ

💰 Сумма: ${amount}
💎 Валюта: USDT

🔗 Ссылка для оплаты:
{invoice['pay_url']}

⏰ Оплатите в течение 1 часа"""
                
                await event.reply(response, parse_mode='html')
                logger.info(f"Инвойс создан: {invoice['pay_url']}")
            else:
                await event.reply("❌ Ошибка создания счета")
                logger.error("Не удалось создать инвойс")
                
        except ValueError:
            await event.reply("❌ Неверная сумма. Используйте: /deposit 100")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {str(e)}")
            logger.error(f"Ошибка в handle_deposit_command: {e}")
    
    async def handle_price_command(self, event, text: str):
        """Обработка команды /price"""
        try:
            parts = text.split()
            if len(parts) != 2:
                await event.reply("❌ Используйте: /price 3.5")
                return
            
            new_price = float(parts[1])
            if new_price <= 0:
                await event.reply("❌ Прайс должен быть больше 0")
                return
            
            self.price = new_price
            self.current_work['price'] = new_price
            await event.reply(f"✅ Прайс изменен на ${new_price}")
            
        except ValueError:
            await event.reply("❌ Неверный формат. Используйте: /price 3.5")
    
    async def handle_time_command(self, event, text: str):
        """Обработка команды /time"""
        try:
            parts = text.split()
            if len(parts) != 2:
                await event.reply("❌ Используйте: /time 5 (минут)")
                return
            
            new_time = int(parts[1])
            if new_time < 1:
                await event.reply("❌ Время должно быть минимум 1 минута")
                return
            
            self.payment_time = new_time
            await event.reply(f"✅ Время выплаты изменено на {new_time} минут")
            
        except ValueError:
            await event.reply("❌ Неверный формат. Используйте: /time 5")
    
    async def handle_reset_command(self, event):
        """Обработка команды /reset - перезагрузка очередей"""
        # Сбрасываем текущую работу
        self.current_work = {
            'phone': None,
            'sender_id': None,
            'sender_username': None,
            'status': 'waiting_number',
            'start_time': None,
            'code_sent_time': None,
            'price': self.price,
            'is_repeat': False
        }
        
        # Очищаем очереди
        self.trigger_queue = []
        self.number_queue = []
        
        # Останавливаем все таймеры автовыплаты
        for key, data in self.active_numbers.items():
            if 'auto_payment_task' in data and data['auto_payment_task']:
                try:
                    data['auto_payment_task'].cancel()
                except:
                    pass
        
        # Очищаем активные номера
        self.active_numbers = {}
        
        await event.reply("✅ Система перезагружена!\n📊 Отчеты сохранены.\n🔄 Очереди очищены.\n⏸️ Текущая работа сброшена.")
        
        # Запрашиваем новый номер
        if self.work_active:
            await self.request_number()
    
    async def handle_phone_group1(self, event, phone: str):
        """Обработка номера из группы 1"""
        # Проверяем размер очереди
        if len(self.number_queue) >= MAX_QUEUE_SIZE:
            await event.reply(f"""🚫 ОЧЕРЕДЬ ПЕРЕПОЛНЕНА!

📊 В очереди уже {MAX_QUEUE_SIZE} номеров.
⏳ Подождите, пока разгрузится очередь.
🔄 Я беру только {MAX_QUEUE_SIZE} номеров одновременно.""")
            return
        
        sender = await event.get_sender()
        username = f"@{sender.username}" if sender.username else f"ID:{sender.id}"
        
        logger.info(f"Получен номер {phone} от {username}")
        
        if self.current_work['status'] == 'waiting_number' and not self.current_work['phone']:
            await self.take_number(event, phone, sender, username)
            
            if self.trigger_queue:
                await self.process_trigger_queue()
        else:
            self.number_queue.append({
                'phone': phone,
                'sender_id': sender.id,
                'sender_username': username,
                'sender_message_id': event.message.id,
                'timestamp': datetime.now()
            })
            
            await event.reply(f"""✅ Номер принят в очередь!

📱 Номер: {phone}
👤 Исполнитель: {username}
📊 Позиция в очереди: {len(self.number_queue)}
💰 Выплата: ${self.price}

⏳ Ожидайте, когда номер будет взят в работу.""")
            
            logger.info(f"Номер {phone} добавлен в очередь")
    
    async def take_number(self, event, phone: str, sender, username: str):
        """Взять номер в работу"""
        self.current_work.update({
            'phone': phone,
            'sender_id': sender.id,
            'sender_username': username,
            'status': 'has_number',
            'start_time': datetime.now(),
            'price': self.price,
            'is_repeat': False
        })
        
        await event.reply(f"""✅ ПРИНЯТ НОМЕР ОТ ДРОПА!

👤 Исполнитель: {username}
📱 Номер: {phone}
💰 Выплата: ${self.price}
⏱ Минимальное время: {self.payment_time} минут

⚠️ Ожидайте запроса кода или пароля!
⏱ Код нужно отправить в течение 2 минут!""")
        
        logger.info(f"Взят номер {phone} в работу")
    
    async def process_number_queue(self):
        """Обработка очереди номеров"""
        if self.current_work['status'] == 'waiting_number' and not self.current_work['phone'] and self.number_queue:
            number_data = self.number_queue.pop(0)
            
            self.current_work.update({
                'phone': number_data['phone'],
                'sender_id': number_data['sender_id'],
                'sender_username': number_data['sender_username'],
                'status': 'has_number',
                'start_time': datetime.now(),
                'price': self.price,
                'is_repeat': False
            })
            
            await self.client.send_message(
                entity=GROUP1_ID,
                message=f"""✅ Взял номер из очереди!

📱 Номер: {number_data['phone']}
👤 Исполнитель: {number_data['sender_username']}
💰 Выплата: ${self.price}
⏱ Минимальное время: {self.payment_time} минут
📊 Осталось в очереди: {len(self.number_queue)}

⚠️ Ожидайте запроса кода!"""
            )
            
            logger.info(f"Взял номер {number_data['phone']} из очереди")
            
            if self.trigger_queue:
                await self.process_trigger_queue()
    
    async def handle_group2_message(self, event):
        """Обработка сообщений в группах 2"""
        text = (event.message.text or '').lower().strip()
        chat_id = event.chat_id
        topic_id = getattr(event.message, 'reply_to_msg_id', None) or 1
        
        logger.info(f"Группа 2 ({chat_id}, топик {topic_id}): '{text}'")
        
        # Ключ для активного номера
        key = (chat_id, topic_id)
        
        # 1. Проверяем команды блокировки
        block_words = ['заблок', 'блок', 'заблокан', 'в блоке', 'бан']
        for word in block_words:
            if word in text:
                logger.info(f"Блокировка: {text}")
                if key in self.active_numbers:
                    phone = self.active_numbers[key]['phone']
                    sender = self.active_numbers[key]['sender_username']
                    
                    await self.client.send_message(
                        entity=GROUP1_ID,
                        message=f"""🚫 НОМЕР ЗАБЛОКИРОВАН!

📱 Номер: {phone}
👤 Исполнитель: {sender}
⏱ Время: {datetime.now().strftime('%H:%M:%S')}

❌ Номер заблокирован в приложении"""
                    )
                    
                    if 'auto_payment_task' in self.active_numbers[key]:
                        try:
                            self.active_numbers[key]['auto_payment_task'].cancel()
                        except:
                            pass
                    
                    del self.active_numbers[key]
                    self.reset_current_work()
                    
                    # Проверяем очередь
                    if len(self.number_queue) < MAX_QUEUE_SIZE:
                        await self.request_number()
                return
        
        # 2. Проверяем активный номер
        if key in self.active_numbers:
            await self.handle_active_chat_message(event, key, text)
            return
        
        # 3. Проверяем, если кто-то дал номер вручную - убираем запрос из очереди
        phone_match = re.search(r'(?:\+7|7|8)\d{10}', text)
        if phone_match:
            # Удаляем этот топик из очереди запросов
            self.trigger_queue = [t for t in self.trigger_queue if not (t['chat_id'] == chat_id and t['topic_id'] == topic_id)]
            return
        
        # 4. Новый запрос номера
        trigger_words = ['номер', 'дай номер', 'нужен номер', 'слет', 'слёт']
        for word in trigger_words:
            if word in text:
                logger.info(f"Запрос номера: {text}")
                
                # Проверяем, нет ли уже такого запроса
                for trigger in self.trigger_queue:
                    if trigger['chat_id'] == chat_id and trigger['topic_id'] == topic_id:
                        await event.reply("⏳ Уже в очереди на номер")
                        return
                
                sender = await event.get_sender()
                username = f"@{sender.username}" if sender.username else f"ID:{sender.id}"
                
                self.trigger_queue.append({
                    'chat_id': chat_id,
                    'topic_id': topic_id,
                    'user_id': sender.id,
                    'username': username,
                    'timestamp': datetime.now(),
                    'is_repeat': False
                })
                
                await event.reply("✅ Запрос номера принят в очередь")
                
                if self.current_work['status'] == 'has_number':
                    await self.process_trigger_queue()
                elif not self.current_work['phone']:
                    await self.request_number()
                return
        
        # 5. Запрос повтора
        if 'повтор' in text:
            logger.info(f"Запрос повтора: {text}")
            if key in self.active_numbers:
                await self.handle_code_request(key)
            return
    
    async def handle_active_chat_message(self, event, key: tuple, text: str):
        """Обработка сообщений в чате с активным номером"""
        logger.info(f"Активный чат {key}: {text}")
        
        # 1. Номер встал
        if '+' in text or any(word in text for word in ['встал', 'готов', 'готово', 'успех']):
            logger.info("Номер встал!")
            await self.handle_number_standup(key)
            return
        
        # 2. Номер слетел
        if any(word in text for word in ['слет', 'слёт', 'ошибка', 'не работает', 'error']):
            logger.info("Номер слетел!")
            await self.handle_number_fall(key, text)
            return
        
        # 3. Запрос кода
        if 'код' in text and 'пароль' not in text:
            logger.info("Запрос кода!")
            await self.handle_code_request(key)
            return
        
        # 4. Запрос пароля
        if 'пароль' in text:
            logger.info("Запрос пароля!")
            await self.handle_password_request(key)
            return
        
        # 5. Запрос повтора кода
        if 'повтор' in text:
            logger.info("Запрос повтора кода!")
            await self.handle_code_request(key)
            return
    
    async def handle_code_request(self, key: tuple):
        """Запрос кода"""
        if key not in self.active_numbers:
            return
            
        phone = self.active_numbers[key]['phone']
        sender = self.active_numbers[key]['sender_username']
        sender_id = self.active_numbers[key]['sender_id']
        
        # Добавляем дропа в контакты
        await self.add_to_contacts(sender_id, phone, sender)
        
        await self.client.send_message(
            entity=GROUP1_ID,
            message=f"""📲 ВНИМАНИЕ {sender}!

На твой номер {phone} пришла SMS с кодом!

⚠️ ВАЖНО: ЕСЛИ У ВАС СПАМБЛОК - ДОБАВЬТЕ МЕНЯ В КОНТАКТЫ И ПИШИТЕ! 
✅ ВАС Я УЖЕ ДОБАВИЛ В СВОИ КОНТАКТЫ!

🚨 НЕ ПИШИ КОД В ЧАТ! Отправь в ЛС!
⏱ У тебя 2 минуты на отправку кода!"""
        )
        
        self.current_work['status'] = 'waiting_code'
        self.current_work['code_sent_time'] = datetime.now()
    
    async def add_to_contacts(self, user_id: int, phone: str, username: str):
        """Добавление пользователя в контакты"""
        if user_id in self.added_contacts:
            return
        
        try:
            # Получаем информацию о пользователе
            try:
                user = await self.client.get_entity(user_id)
                first_name = user.first_name or ""
                last_name = user.last_name or ""
            except:
                first_name = username.replace("@", "")
                last_name = ""
            
            # Создаем контакт
            contact = InputPhoneContact(
                client_id=0,
                phone=phone,
                first_name=first_name,
                last_name=last_name
            )
            
            # Импортируем контакт
            result = await self.client(ImportContactsRequest([contact]))
            
            if result.users:
                self.added_contacts.add(user_id)
                logger.info(f"Добавлен в контакты: {username} ({phone})")
                return True
            
        except Exception as e:
            logger.error(f"Ошибка добавления в контакты: {e}")
        
        return False
    
    async def handle_password_request(self, key: tuple):
        """Запрос пароля"""
        if key not in self.active_numbers:
            return
            
        phone = self.active_numbers[key]['phone']
        sender = self.active_numbers[key]['sender_username']
        sender_id = self.active_numbers[key]['sender_id']
        
        # Добавляем дропа в контакты
        await self.add_to_contacts(sender_id, phone, sender)
        
        await self.client.send_message(
            entity=GROUP1_ID,
            message=f"""🔐 ВНИМАНИЕ {sender}!

Для номера {phone} нужен пароль!

⚠️ ВАЖНО: ЕСЛИ У ВАС СПАМБЛОК - ДОБАВЬТЕ МЕНЯ В КОНТАКТЫ И ПИШИТЕ! 
✅ ВАС Я УЖЕ ДОБАВИЛ В СВОИ КОНТАКТЫ!

🚨 НЕ ПИШИ ПАРОЛЬ В ЧАТ! Отправь в ЛС!
⏱ У тебя 2 минуты на отправку пароля!"""
        )
        
        self.current_work['status'] = 'waiting_password'
        self.current_work['code_sent_time'] = datetime.now()
    
    async def handle_number_standup(self, key: tuple):
        """Номер встал"""
        if key not in self.active_numbers:
            return
            
        chat_id, topic_id = key
        phone = self.active_numbers[key]['phone']
        sender = self.active_numbers[key]['sender_username']
        
        self.active_numbers[key]['standup_time'] = datetime.now()
        
        # Обновляем статистику группы 2
        if key not in self.group2_stats:
            self.group2_stats[key] = {'total': 0, 'success': 0}
        self.group2_stats[key]['total'] += 1
        
        # Обновляем отчет в группе 2
        await self.update_group2_report(chat_id, phone, sender, 'success')
        
        await self.client.send_message(
            entity=GROUP1_ID,
            message=f"""✅ НОМЕР УСПЕШНО ПРИВЯЗАН!

📱 Номер: {phone}
👤 Исполнитель: {sender}
⏱ Время привязки: {datetime.now().strftime('%H:%M:%S')}
💰 Автовыплата через {self.payment_time} минут!"""
        )
        
        # Запускаем таймер автовыплаты
        self.active_numbers[key]['auto_payment_task'] = asyncio.create_task(
            self.start_auto_payment_timer(key)
        )
        
        self.reset_current_work()
        
        # Проверяем очередь перед запросом нового номера
        if len(self.number_queue) < MAX_QUEUE_SIZE:
            await self.request_number()
        
        if self.trigger_queue and self.current_work['status'] == 'has_number':
            await self.process_trigger_queue()
    
    async def update_group2_report(self, chat_id: int, phone: str, sender: str, status: str):
        """Обновление отчета в группе 2"""
        if chat_id not in GROUP2_REPORT_TOPICS:
            return
        
        report_topic_id = GROUP2_REPORT_TOPICS[chat_id]
        report_key = (chat_id, report_topic_id)
        
        try:
            # Получаем текущий отчет
            if report_key in self.report_messages:
                # Редактируем существующее сообщение
                message_id = self.report_messages[report_key]
                try:
                    message = await self.client.get_messages(
                        chat_id, 
                        ids=message_id
                    )
                    if message:
                        # Парсим текущий текст и добавляем новый номер
                        current_text = message.text or ""
                        lines = current_text.strip().split('\n')
                        
                        # Находим последний номер
                        last_num = 0
                        for line in lines:
                            if line.strip().startswith(tuple(str(i) for i in range(1, 10))):
                                try:
                                    num = int(line.split('.')[0].strip())
                                    last_num = max(last_num, num)
                                except:
                                    pass
                        
                        new_num = last_num + 1
                        new_line = f"{new_num}. {phone} | {sender} | ⏱"
                        
                        if current_text.strip():
                            new_text = f"{current_text}\n{new_line}"
                        else:
                            new_text = f"📊 ОТЧЕТ ПО НОМЕРАМ:\n\n{new_line}"
                        
                        await self.client.edit_message(
                            chat_id, 
                            message_id, 
                            new_text
                        )
                        return
                except:
                    pass
            
            # Создаем новое сообщение
            text = f"""📊 ОТЧЕТ ПО НОМЕРАМ:

1. {phone} | {sender} | ⏱"""
            message = await self.client.send_message(
                entity=chat_id,
                message=text,
                reply_to=report_topic_id
            )
            self.report_messages[report_key] = message.id
            
        except Exception as e:
            logger.error(f"Ошибка обновления отчета в группе 2: {e}")
    
    async def start_auto_payment_timer(self, key: tuple):
        """Таймер автовыплаты"""
        await asyncio.sleep(self.payment_time * 60)  # Конвертируем минуты в секунды
        
        if key in self.active_numbers and 'standup_time' in self.active_numbers[key]:
            await self.send_auto_payment(key)
    
    async def send_auto_payment(self, key: tuple):
        """Отправка автовыплаты"""
        if key not in self.active_numbers:
            return
            
        data = self.active_numbers[key]
        phone = data['phone']
        sender = data['sender_username']
        sender_id = data['sender_id']
        standup_time = data['standup_time']
        fall_time = datetime.now()
        
        duration = (fall_time - standup_time).total_seconds()
        duration_minutes = int(duration // 60)
        
        await self.update_balance()
        
        if self.balance >= self.price:
            check = await self.crypto_pay.create_check(sender_id, self.price)
            
            if check:
                self.active_numbers[key]['payment_sent'] = True
                
                # Обновляем статистику
                if key in self.group2_stats:
                    self.group2_stats[key]['success'] += 1
                
                check_text = f"""💸 ВЫПЛАТА ЗА РАБОТУ!

📱 Номер: {phone}
👤 Исполнитель: {sender}
⏱ Время работы: {duration_minutes} минут
💰 Сумма выплаты: ${self.price}

🎁 Забери свою выплату:
{check['bot_check_url']}"""
                
                # Отправляем выплату в топик для выплат
                await self.client.send_message(
                    entity=GROUP1_ID,
                    message=check_text,
                    reply_to=GROUP1_PAYMENT_TOPIC_ID
                )
                
                self.balance -= self.price
                
                report = {
                    'phone': phone,
                    'sender': sender,
                    'standup_time': standup_time.strftime('%H:%M'),
                    'fall_time': fall_time.strftime('%H:%M'),
                    'duration_minutes': duration_minutes,
                    'result': 'success_auto',
                    'price': self.price,
                    'check_url': check['bot_check_url'],
                    'date': datetime.now().strftime('%Y-%m-%d')
                }
                self.reports.append(report)
                
                await self.client.send_message(
                    entity=GROUP1_ID,
                    message=f"""✅ ВЫПЛАТА ОТПРАВЛЕНА!

📱 Номер: {phone}
👤 Исполнитель: {sender}
💰 Сумма: ${self.price}
⏱ Время: {duration_minutes} минут"""
                )
                
                logger.info(f"Автовыплата {phone}: {duration_minutes} мин, ${self.price}")
                
                if key in self.active_numbers:
                    del self.active_numbers[key]
            else:
                logger.error(f"Ошибка создания чека для {phone}")
        else:
            logger.error(f"Недостаточно баланса для выплаты {phone}")
    
    async def handle_number_fall(self, key: tuple, reason: str):
        """Номер слетел"""
        if key not in self.active_numbers:
            return
            
        chat_id, topic_id = key
        phone = self.active_numbers[key]['phone']
        sender = self.active_numbers[key]['sender_username']
        sender_id = self.active_numbers[key]['sender_id']
        
        # Обновляем статистику
        if key not in self.group2_stats:
            self.group2_stats[key] = {'total': 0, 'success': 0}
        self.group2_stats[key]['total'] += 1
        
        # Обновляем отчет в группе 2
        await self.update_group2_report(chat_id, phone, sender, 'fall')
        
        if 'auto_payment_task' in self.active_numbers[key]:
            try:
                self.active_numbers[key]['auto_payment_task'].cancel()
            except:
                pass
        
        if 'standup_time' in self.active_numbers[key]:
            standup_time = self.active_numbers[key]['standup_time']
            fall_time = datetime.now()
            
            duration = (fall_time - standup_time).total_seconds()
            duration_minutes = int(duration // 60)
            
            # Проверяем минимальное время для выплаты
            if duration_minutes >= self.payment_time:
                await self.update_balance()
                if self.balance >= self.price:
                    check = await self.crypto_pay.create_check(sender_id, self.price)
                    
                    if check:
                        self.balance -= self.price
                        
                        # Обновляем статистику
                        self.group2_stats[key]['success'] += 1
                        
                        check_text = f"""💸 ВЫПЛАТА ЗА СЛЕТЕВШИЙ НОМЕР

📱 Номер: {phone}
👤 Исполнитель: {sender}
⏱ Время работы: {duration_minutes} минут
💰 Сумма выплаты: ${self.price}

🎁 Забери выплату:
{check['bot_check_url']}"""
                        
                        await self.client.send_message(
                            entity=GROUP1_ID,
                            message=check_text,
                            reply_to=GROUP1_PAYMENT_TOPIC_ID
                        )
        
        await self.client.send_message(
            entity=GROUP1_ID,
            message=f"""❌ НОМЕР СЛЕТЕЛ!

📱 Номер: {phone}
👤 Исполнитель: {sender}
⏱ Время работы: {duration_minutes} минут
📝 Причина: {reason}"""
        )
        
        if key in self.active_numbers:
            del self.active_numbers[key]
        
        self.reset_current_work()
        
        # Проверяем очередь перед запросом нового номера
        if len(self.number_queue) < MAX_QUEUE_SIZE:
            await self.request_number()
        
        if self.trigger_queue and self.current_work['status'] == 'has_number':
            await self.process_trigger_queue()
    
    async def handle_private_message(self, event):
        """Обработка личных сообщений"""
        text = event.message.text.strip()
        user_id = event.sender_id
        
        sender = await event.get_sender()
        sender_name = f"@{sender.username}" if sender.username else f"ID:{sender.id}"
        
        # Код (6 цифр)
        if len(text) == 6 and text.isdigit():
            for key, data in self.active_numbers.items():
                if data.get('sender_id') == user_id:
                    phone = data['phone']
                    chat_id, topic_id = key
                    
                    # Отправляем код в тот же топик, откуда запрос - ПРОСТО "КОД"
                    await self.client.send_message(
                        entity=chat_id,
                        message=f"код\n{text}",
                        reply_to=topic_id
                    )
                    
                    await event.reply("✅ Код принят и отправлен в рабочую группу!")
                    self.current_work['code_sent_time'] = None
                    return
            
            await event.reply("❌ У тебя нет активных номеров в работе")
            return
        
        # Пароль (минимум 4 символа)
        elif len(text) >= 4:
            for key, data in self.active_numbers.items():
                if data.get('sender_id') == user_id:
                    phone = data['phone']
                    chat_id, topic_id = key
                    
                    # Отправляем пароль в тот же топик, откуда запрос - ПРОСТО "ПАРОЛЬ"
                    await self.client.send_message(
                        entity=chat_id,
                        message=f"пароль\n{text}",
                        reply_to=topic_id
                    )
                    
                    await event.reply("✅ Пароль принят и отправлен в рабочую группу!")
                    self.current_work['code_sent_time'] = None
                    return
            
            await event.reply("❌ У тебя нет активных номеров в работе")
    
    async def cmd_start(self, event):
        """Команда /start"""
        text = """👋 ПРИВЕТ!

Я - автоматизированная система для работы с номерами.

Основные команды:
/report - отчет за сегодня
/stats - полная статистика  
/balance - баланс и статус
/otchet - полный отчет с деталями
/help - помощь

Для админов:
/adm - админ панель
/deposit 100 - пополнить баланс
/price 3.5 - изменить прайс
/time 5 - изменить время выплаты
/reset - перезагрузка системы
/stop - остановить работу
/startwork - возобновить работу"""
        
        await event.reply(text)
    
    async def cmd_admin(self, event):
        """Команда /adm"""
        user_id = event.sender_id
        
        if user_id not in ADMIN_IDS:
            await event.reply("🚫 Доступ запрещен")
            return
        
        await self.update_balance()
        
        today = datetime.now().strftime('%Y-%m-%d')
        today_reports = [r for r in self.reports if r.get('date') == today]
        
        total_today = len(today_reports)
        success_today = len([r for r in today_reports if r.get('result') in ['success_auto', 'success_manual']])
        payments_today = sum(r['price'] for r in today_reports if r.get('result') in ['success_auto', 'success_manual'])
        
        text = f"""⚙️ АДМИН ПАНЕЛЬ

Статус: {'🟢 Активна' if self.work_active else '🔴 Остановлена'}
Прайс: ${self.price}
Время выплаты: от {self.payment_time} минут
Макс. очередь: {MAX_QUEUE_SIZE} номеров
Текущий номер: {self.current_work['phone'] or 'нет'}
Активных номеров: {len(self.active_numbers)}

📊 ОЧЕРЕДИ:
Номеров в очереди: {len(self.number_queue)}
Запросов в очереди: {len(self.trigger_queue)}

Баланс: ${self.balance:.2f}

Сегодня ({today}):
Всего номеров: {total_today}
Успешно: {success_today}
Выплачено: ${payments_today:.2f}

Команды:
/deposit 100 - пополнить баланс
/price 3.5 - изменить прайс  
/time 5 - изменить время выплаты
/reset - перезагрузка системы
/stop - остановить работу
/startwork - возобновить работу
/queue - показать очереди"""
        
        await event.reply(text)
    
    async def cmd_queue(self, event):
        """Показать очереди"""
        text = f"""📊 СТАТУС ОЧЕРЕДЕЙ

🎯 МАКСИМАЛЬНЫЙ РАЗМЕР: {MAX_QUEUE_SIZE} номеров

📞 ОЧЕРЕДЬ НОМЕРОВ ({len(self.number_queue)}/{MAX_QUEUE_SIZE}):"""
        
        if self.number_queue:
            for i, num in enumerate(self.number_queue, 1):
                text += f"\n{i}. {num['phone']} | {num['sender_username']}"
        else:
            text += "\n📭 Очередь номеров пуста"
        
        text += f"\n\n📝 ОЧЕРЕДЬ ЗАПРОСОВ ({len(self.trigger_queue)}):"
        
        if self.trigger_queue:
            for i, trigger in enumerate(self.trigger_queue, 1):
                text += f"\n{i}. Группа {trigger['chat_id']} | Топик {trigger['topic_id']}"
        else:
            text += "\n📭 Очередь запросов пуста"
        
        text += f"\n\n📱 ТЕКУЩАЯ РАБОТА:"
        text += f"\nНомер: {self.current_work['phone'] or 'нет'}"
        text += f"\nСтатус: {self.current_work['status']}"
        text += f"\nИсполнитель: {self.current_work['sender_username'] or 'нет'}"
        
        await event.reply(text)
    
    async def cmd_otchet(self, event):
        """Команда /otchet - полный отчет"""
        await self.update_balance()
        
        today = datetime.now().strftime('%Y-%m-%d')
        today_reports = [r for r in self.reports if r.get('date') == today]
        
        # Статистика по группам 2
        group_stats_text = ""
        for (chat_id, topic_id), stats in self.group2_stats.items():
            group_stats_text += f"\n  • Группа {chat_id} (топик {topic_id}):"
            group_stats_text += f"\n    Всего номеров: {stats.get('total', 0)}"
            group_stats_text += f"\n    Успешно: {stats.get('success', 0)}"
            group_stats_text += f"\n    Процент: {stats.get('success', 0) / stats.get('total', 1) * 100:.1f}%"
        
        total_today = len(today_reports)
        success_today = len([r for r in today_reports if r.get('result') in ['success_auto', 'success_manual']])
        short_today = len([r for r in today_reports if r.get('result') in ['short', 'short_fall']])
        falls_today = len([r for r in today_reports if r.get('result') in ['fall', 'fall_after_payment']])
        timeouts_today = len([r for r in today_reports if r.get('result') == 'code_timeout'])
        payments_today = sum(r['price'] for r in today_reports if r.get('result') in ['success_auto', 'success_manual'])
        
        total_all = len(self.reports)
        success_all = len([r for r in self.reports if r.get('result') in ['success_auto', 'success_manual']])
        payments_all = sum(r['price'] for r in self.reports if r.get('result') in ['success_auto', 'success_manual'])
        
        text = f"""📊 ПОЛНЫЙ ОТЧЕТ

📅 Дата: {today}
💰 Баланс: ${self.balance:.2f}
💵 Прайс: ${self.price}
⏱ Минимальное время: {self.payment_time} минут
🔄 Статус: {'🟢 Активен' if self.work_active else '🔴 Остановлен'}

📈 СЕГОДНЯ ({today}):
📞 Всего номеров: {total_today}
✅ Успешно: {success_today}
⚠️ Короткие: {short_today}
❌ Слетов: {falls_today}
⏰ Таймаутов: {timeouts_today}
💸 Выплачено: ${payments_today:.2f}

📊 ВСЕГО:
📞 Всего номеров: {total_all}
✅ Успешно: {success_all}
💸 Все выплаты: ${payments_all:.2f}

📱 СТАТИСТИКА ПО ГРУППАМ 2:{group_stats_text if group_stats_text else "/n  • Нет данных"}

📱 АКТИВНЫЕ НОМЕРА ПО ГРУППАМ:"""
        
        group_active_stats = {}
        for (chat_id, topic_id), data in self.active_numbers.items():
            if chat_id not in group_active_stats:
                group_active_stats[chat_id] = 0
            group_active_stats[chat_id] += 1
        
        for chat_id, count in group_active_stats.items():
            text += f"\n  • Группа {chat_id}: {count} номеров"
        
        if not group_active_stats:
            text += "\n  • Нет активных номеров"
        
        text += f"\n\n👥 Всего активных: {len(self.active_numbers)}"
        text += f"\n⏳ Очередь номеров: {len(self.number_queue)}/{MAX_QUEUE_SIZE}"
        text += f"\n🎯 Очередь запросов: {len(self.trigger_queue)}"
        text += f"\n📞 Текущий номер: {self.current_work['phone'] or 'нет'}"
        
        # Последние 5 отчетов
        if today_reports:
            text += f"\n\n📝 ПОСЛЕДНИЕ ОТЧЕТЫ:"
            for i, report in enumerate(today_reports[-5:], 1):
                if report.get('result') in ['success_auto', 'success_manual']:
                    status = f"✅ {report.get('duration_minutes', 0)} мин"
                    price = f"${report.get('price', 0)}"
                else:
                    status = "❌ Слет"
                    price = "$0"
                
                text += f"\n{i}. {report.get('phone', '?')}"
                text += f" | {report.get('sender', '?')}"
                text += f" | {status} | {price}"
        
        await event.reply(text)
    
    async def cmd_report(self, event):
        """Отчет за сегодня с подробностями"""
        today = datetime.now().strftime('%Y-%m-%d')
        today_reports = [r for r in self.reports if r.get('date') == today]
        
        if not today_reports:
            await event.reply(f"📊 Отчет за {today}\n\nНет данных за сегодня.")
            return
        
        text = f"📊 ОТЧЕТ ЗА {today}\n\n"
        
        for i, report in enumerate(today_reports, 1):
            if report.get('result') in ['success_auto', 'success_manual']:
                status = f"✅ {report.get('duration_minutes', 0)} мин"
                price = f"${report.get('price', 0)}"
            elif report.get('result') in ['short', 'short_fall']:
                status = f"⚠️ {report.get('duration_minutes', 0)} мин"
                price = "$0"
            elif report.get('result') == 'code_timeout':
                status = "⏰ Таймаут кода"
                price = "$0"
            else:
                status = "❌ Слет"
                price = "$0"
            
            text += f"{i}. {report.get('phone', '?')}\n"
            text += f"👤 {report.get('sender', '?')}\n"
            text += f"⏱ {report.get('standup_time', '?')} - {report.get('fall_time', '?')}\n"
            text += f"{status} | {price}\n\n"
        
        await event.reply(text)
    
    async def cmd_stats(self, event):
        """Статистика"""
        today = datetime.now().strftime('%Y-%m-%d')
        today_reports = [r for r in self.reports if r.get('date') == today]
        
        total_today = len(today_reports)
        success_today = len([r for r in today_reports if r.get('result') in ['success_auto', 'success_manual']])
        short_today = len([r for r in today_reports if r.get('result') in ['short', 'short_fall']])
        falls_today = len([r for r in today_reports if r.get('result') in ['fall', 'fall_after_payment']])
        timeouts_today = len([r for r in today_reports if r.get('result') == 'code_timeout'])
        payments_today = sum(r['price'] for r in today_reports if r.get('result') in ['success_auto', 'success_manual'])
        
        total_all = len(self.reports)
        success_all = len([r for r in self.reports if r.get('result') in ['success_auto', 'success_manual']])
        payments_all = sum(r['price'] for r in self.reports if r.get('result') in ['success_auto', 'success_manual'])
        
        text = f"""📈 СТАТИСТИКА

Сегодня ({today}):
Всего номеров: {total_today}
Успешно: {success_today}
Короткие: {short_today}
Слетов: {falls_today}
Таймаутов: {timeouts_today}
Выплачено: ${payments_today:.2f}

Всего:
Всего номеров: {total_all}
Успешно: {success_all}
Все выплаты: ${payments_all:.2f}

Текущее:
Прайс: ${self.price}
Время выплаты: {self.payment_time} минут
Баланс: ${self.balance:.2f}
Статус: {'Активен' if self.work_active else 'Остановлен'}
Активных номеров: {len(self.active_numbers)}"""
        
        await event.reply(text)
    
    async def cmd_balance(self, event):
        """Баланс"""
        await self.update_balance()
        
        text = f"""💰 БАЛАНС

Текущий баланс: ${self.balance:.2f}
Текущий прайс: ${self.price}
Время выплаты: от {self.payment_time} минут
Доступно выплат: {int(self.balance / self.price) if self.price > 0 else 0}

Статус работы: {'Активен' if self.work_active else 'Остановлен'}
Активных номеров: {len(self.active_numbers)}
Очередь номеров: {len(self.number_queue)}/{MAX_QUEUE_SIZE}"""
        
        await event.reply(text)
    
    async def cmd_active(self, event):
        """Активные номера"""
        if not self.active_numbers:
            await event.reply("📱 АКТИВНЫЕ НОМЕРА\n\nНет активных номеров.")
            return
        
        text = "📱 АКТИВНЫЕ НОМЕРА\n\n"
        
        for i, (key, data) in enumerate(self.active_numbers.items(), 1):
            chat_id, topic_id = key
            phone = data['phone']
            sender = data['sender_username']
            
            if 'standup_time' in data:
                status = "🟢 В работе"
                duration = f"{(datetime.now() - data['standup_time']).total_seconds() / 60:.1f} мин"
            else:
                status = "🟡 Ожидает кода"
                duration = "ожидание"
            
            text += f"{i}. {phone}\n"
            text += f"👤 {sender}\n"
            text += f"💬 Группа: {chat_id} (топик {topic_id})\n"
            text += f"{status} | {duration}\n\n"
        
        await event.reply(text)
    
    async def cmd_help(self, event):
        """Помощь"""
        text = """🤖 ПОМОЩЬ

Команды в избранном:
/start - начало работы
/report - отчет за сегодня
/stats - полная статистика
/balance - баланс и статус
/otchet - полный отчет с деталями
/active - активные номера
/queue - показать очереди
/help - эта справка

Команды админа:
/adm - админ панель
/deposit 100 - пополнить баланс
/price 3.5 - изменить прайс
/time 5 - изменить время выплаты
/reset - перезагрузка системы
/stop - остановить работу
/startwork - возобновить работу

Команды в группах:
"номер" - запросить номер
"код" - запросить код
"пароль" - запросить пароль
"+" - номер привязался
"слет" - номер слетел
"заблок" - номер заблокирован
"повтор" - запросить повтор кода"""
        
        await event.reply(text)
    
    async def process_trigger_queue(self):
        """Обработка очереди триггеров"""
        if not self.trigger_queue or self.current_work['status'] != 'has_number':
            return
        
        trigger = self.trigger_queue.pop(0)
        await self.send_number_to_group2(trigger)
    
    async def send_number_to_group2(self, trigger: dict):
        """Отправка номера в группу 2"""
        phone = self.current_work['phone']
        chat_id = trigger['chat_id']
        topic_id = trigger['topic_id']
        is_repeat = trigger.get('is_repeat', False)
        
        if is_repeat:
            text = f"повтор\n{phone}"
        else:
            text = f"номер\n{phone}"
        
        try:
            message = await self.client.send_message(
                entity=chat_id,
                message=text,
                reply_to=topic_id
            )
            
            self.current_work['status'] = 'number_sent'
            
            key = (chat_id, topic_id)
            self.active_numbers[key] = {
                'phone': phone,
                'sender_username': self.current_work['sender_username'],
                'sender_id': self.current_work['sender_id'],
                'chat_id': chat_id,
                'topic_id': topic_id,
                'message_id': message.id,
                'sent_time': datetime.now(),
                'trigger_username': trigger['username'],
                'is_repeat': is_repeat,
                'auto_payment_task': None
            }
            
            logger.info(f"Отправил номер {phone} в чат {chat_id} (топик {topic_id})")
            
        except Exception as e:
            logger.error(f"Ошибка отправки номера: {e}")
            self.trigger_queue.insert(0, trigger)
    
    async def request_number(self):
        """Запрос номера в группе 1"""
        try:
            # Проверяем очередь
            queue_status = f"\n📊 Очередь: {len(self.number_queue)}/{MAX_QUEUE_SIZE} номеров"
            if len(self.number_queue) >= MAX_QUEUE_SIZE:
                queue_status = f"\n🚫 ОЧЕРЕДЬ ПЕРЕПОЛНЕНА! Не принимаю новые номера"
            
            text = f"""📱 РЕБЯТА, НУЖЕН НОМЕР!

Кто может скинуть номер для работы?
💰 Оплата сразу после привязки!

📞 Формат: 79XXXXXXXXX
🌍 Страна: Россия
📶 Оператор: любой

⏱ Выплата через {self.payment_time} минут после привязки!
💵 Сумма выплаты: ${self.price}
{queue_status}

Жду номера в этом чате!"""
            
            await self.client.send_message(
                entity=GROUP1_ID,
                message=text,
                reply_to=GROUP1_TOPIC_ID
            )
            logger.info("Запросил номер в группе 1")
        except Exception as e:
            logger.error(f"Ошибка запроса номера: {e}")
    
    def reset_current_work(self):
        """Сброс текущей работы"""
        self.current_work = {
            'phone': None,
            'sender_id': None,
            'sender_username': None,
            'status': 'waiting_number',
            'start_time': None,
            'code_sent_time': None,
            'price': self.price,
            'is_repeat': False
        }
    
    async def update_balance(self):
        """Обновление баланса"""
        balance = await self.crypto_pay.get_balance()
        if balance is not None:
            self.balance = balance
            logger.info(f"Баланс обновлен: ${balance}")
    
    async def check_pending_invoices(self):
        """Проверка ожидающих инвойсов"""
        to_remove = []
        
        for invoice_id, data in list(self.pending_invoices.items()):
            if (datetime.now() - data['created']).total_seconds() > 30:
                status = await self.crypto_pay.check_invoice(invoice_id)
                
                if status == 'paid':
                    amount = data['amount']
                    self.balance += amount
                    
                    admin_id = data['admin_id']
                    try:
                        await self.client.send_message(
                            entity=admin_id,
                            message=f"✅ БАЛАНС ПОПОЛНЕН!\nСумма: ${amount}\nНовый баланс: ${self.balance:.2f}"
                        )
                    except:
                        pass
                    
                    to_remove.append(invoice_id)
                    
                elif status in ['expired', 'failed']:
                    to_remove.append(invoice_id)
        
        for invoice_id in to_remove:
            if invoice_id in self.pending_invoices:
                del self.pending_invoices[invoice_id]
    
    async def handle_code_timeout(self):
        """Таймаут кода"""
        if not self.current_work['phone']:
            return
        
        phone = self.current_work['phone']
        sender = self.current_work['sender_username']
        
        await self.client.send_message(
            entity=GROUP1_ID,
            message=f"""⏰ ТАЙМАУТ КОДА!

📱 Номер: {phone}
👤 Исполнитель: {sender}
⏱ Время ожидания: 2 минуты

❌ Код не был отправлен вовремя.
🔄 Пропускаю номер и запрашиваю новый!"""
        )
        
        report = {
            'phone': phone,
            'sender': sender,
            'result': 'code_timeout',
            'price': 0,
            'date': datetime.now().strftime('%Y-%m-%d')
        }
        self.reports.append(report)
        
        self.reset_current_work()
        
        # Проверяем очередь перед запросом нового номера
        if len(self.number_queue) < MAX_QUEUE_SIZE:
            await self.request_number()
        
        if self.trigger_queue and self.current_work['status'] == 'has_number':
            await self.process_trigger_queue()

async def main():
    """Точка входа"""
    bot = AccountBot()
    
    try:
        await bot.start()
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        logger.info("Бот остановлен")

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
    finally:
        loop.close()
