import os
import sys
import time
import json
import logging
import asyncio
import sqlite3
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import aiohttp

# ============================================
# 🔧 КОНФИГУРАЦИЯ
# ============================================
CONFIG = {
    "TELEGRAM_TOKEN": "8966478126:AAGWyysf11NWBpd-xQAH-SpdXwHkmUDVx3w",
    "ADMIN_ID": "1601862454",
    "CRYPTO_PAY_TOKEN": "599141:AAz6SFo3NwujrlIEbvQnnxUDNjzGm06U4yk",
    "PRICES": {"1": 49.99, "3": 129.99, "6": 249.99, "12": 499.99},
    "SYMBOLS": ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","LINKUSDT"],
    "CHECK_INTERVAL": 120,
}

MODES = {
    "machinegun": {
        "name": "Пулемёт", "emoji": "🔵", "signal_range": "10-20", "accuracy": "60%",
        "trial_days": 7, "for_who": "скальперов",
        "ADX_THRESHOLD": 15, "MIN_AGREEMENT": 45,
        "SWEEP_LOOKBACK": 12, "SWEEP_SENSITIVITY": 0.004, "SWEEP_WICK_RATIO": 0.4,
        "POC_LOOKBACK": 16, "POC_GRID_SIZE": 0.0035, "POC_CONFIRMATION_ZONE": 0.002,
        "DELTA_LOOKBACK": 10, "DELTA_WICK_WEIGHT": 2.0, "DELTA_CONFIRMATION_RATIO": 1.15,
    },
    "hunter": {
        "name": "Охотник", "emoji": "🟡", "signal_range": "3-8", "accuracy": "75%",
        "trial_days": 14, "for_who": "дей-трейдеров",
        "ADX_THRESHOLD": 20, "MIN_AGREEMENT": 60,
        "SWEEP_LOOKBACK": 20, "SWEEP_SENSITIVITY": 0.0025, "SWEEP_WICK_RATIO": 0.6,
        "POC_LOOKBACK": 24, "POC_GRID_SIZE": 0.0025, "POC_CONFIRMATION_ZONE": 0.003,
        "DELTA_LOOKBACK": 14, "DELTA_WICK_WEIGHT": 2.5, "DELTA_CONFIRMATION_RATIO": 1.3,
    },
    "sniper": {
        "name": "Снайпер", "emoji": "🟠", "signal_range": "0-3", "accuracy": "95%",
        "trial_days": 30, "for_who": "свинг-трейдеров",
        "ADX_THRESHOLD": 25, "MIN_AGREEMENT": 75,
        "SWEEP_LOOKBACK": 30, "SWEEP_SENSITIVITY": 0.0015, "SWEEP_WICK_RATIO": 0.8,
        "POC_LOOKBACK": 30, "POC_GRID_SIZE": 0.002, "POC_CONFIRMATION_ZONE": 0.005,
        "DELTA_LOOKBACK": 18, "DELTA_WICK_WEIGHT": 3.0, "DELTA_CONFIRMATION_RATIO": 1.5,
    }
}

PROXY = "http://proxy.server:3128"
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================
# 📊 БАЗА ДАННЫХ
# ============================================
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, joined_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER, mode TEXT, subscribed_until TEXT,
                  is_trial INTEGER DEFAULT 0, created TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS invoices
                 (invoice_id TEXT PRIMARY KEY, user_id INTEGER,
                  amount REAL, months INTEGER, mode TEXT,
                  status TEXT DEFAULT 'pending', created TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER, amount REAL, months INTEGER,
                  mode TEXT, date TEXT)''')
    conn.commit()
    conn.close()

def register_user(uid, un):
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id=?', (uid,))
    if not c.fetchone():
        c.execute('INSERT INTO users VALUES (?,?,?)', (uid, un, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_active_modes(uid):
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    c.execute('SELECT DISTINCT mode FROM subscriptions WHERE user_id=? AND subscribed_until > ?',
              (uid, datetime.now().isoformat()))
    rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

def has_any_access(uid):
    return len(get_active_modes(uid)) > 0

def start_trial(uid, mode):
    trial_days = MODES[mode]['trial_days']
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    end = datetime.now() + timedelta(days=trial_days)
    c.execute('''INSERT INTO subscriptions (user_id, mode, subscribed_until, is_trial, created)
                 VALUES (?, ?, ?, 1, ?)''',
              (uid, mode, end.isoformat(), datetime.now().isoformat()))
    conn.commit(); conn.close()

def add_subscription(uid, mode, months, amount=0):
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    cur = datetime.now()
    c.execute('SELECT subscribed_until FROM subscriptions WHERE user_id=? AND mode=? AND subscribed_until > ? ORDER BY subscribed_until DESC LIMIT 1',
              (uid, mode, datetime.now().isoformat()))
    row = c.fetchone()
    if row:
        try:
            ec = datetime.fromisoformat(row[0])
            if ec > cur: cur = ec
        except: pass
    new_end = cur + timedelta(days=30 * months)
    c.execute('''INSERT INTO subscriptions (user_id, mode, subscribed_until, is_trial, created)
                 VALUES (?, ?, ?, 0, ?)''',
              (uid, mode, new_end.isoformat(), datetime.now().isoformat()))
    if amount > 0:
        c.execute('''INSERT INTO payments (user_id, amount, months, mode, date)
                     VALUES (?, ?, ?, ?, ?)''',
                  (uid, amount, months, mode, datetime.now().isoformat()))
    conn.commit(); conn.close()

def get_admin_stats():
    conn = sqlite3.connect('users.db'); c = conn.cursor()
    c.execute('SELECT COUNT(DISTINCT user_id) FROM users'); total = c.fetchone()[0]
    c.execute('SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE subscribed_until > ?',
              (datetime.now().isoformat(),)); active = c.fetchone()[0]
    c.execute('SELECT COALESCE(SUM(amount), 0) FROM payments'); revenue = c.fetchone()[0]
    mode_stats = {}
    for mode in MODES:
        c.execute('SELECT COUNT(*), COALESCE(SUM(months), 0) FROM payments WHERE mode=?', (mode,))
        pc, pm = c.fetchone()
        c.execute('SELECT COUNT(*) FROM subscriptions WHERE mode=? AND is_trial=1', (mode,))
        tc = c.fetchone()[0]
        mode_stats[mode] = {'paid_count': pc or 0, 'total_months': pm or 0, 'trials': tc or 0}
    conn.close()
    return total, active, revenue, mode_stats

init_db()

# ============================================
# 💳 CRYPTO BOT
# ============================================
async def create_invoice(uid, months, mode):
    if CONFIG['CRYPTO_PAY_TOKEN'] == '599141:AAz6SFo3NwujrlIEbvQnnxUDNjzGm06U4yk': return None
    amount = CONFIG['PRICES'].get(str(months), 49.99)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://pay.crypt.bot/api/createInvoice",
                headers={"Crypto-Pay-API-Token": CONFIG['CRYPTO_PAY_TOKEN']},
                json={"asset": "USDT", "amount": str(amount),
                      "description": f"CS Pro {MODES[mode]['name']} {months}m",
                      "payload": f"sub_{uid}_{mode}_{months}",
                      "allow_comments": False, "allow_anonymous": False, "expires_in": 3600}
            ) as r:
                res = await r.json()
                if res.get('ok'):
                    inv = res['result']
                    conn = sqlite3.connect('users.db'); c = conn.cursor()
                    c.execute('''INSERT INTO invoices (invoice_id, user_id, amount, months, mode, status, created)
                                 VALUES (?,?,?,?,?,'pending',?)''',
                              (str(inv['invoice_id']), uid, amount, months, mode, datetime.now().isoformat()))
                    conn.commit(); conn.close()
                    return {'url': inv['bot_invoice_url'], 'id': inv['invoice_id']}
    except Exception as e: logger.error(f"Invoice: {e}")
    return None

async def check_invoice(iid):
    if CONFIG['CRYPTO_PAY_TOKEN'] == '599141:AAz6SFo3NwujrlIEbvQnnxUDNjzGm06U4yk': return False, None, None, None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://pay.crypt.bot/api/getInvoices",
                             headers={"Crypto-Pay-API-Token": CONFIG['CRYPTO_PAY_TOKEN']},
                             params={"invoice_ids": iid}) as r:
                res = await r.json()
                if res.get('ok') and res['result']['items']:
                    inv = res['result']['items'][0]
                    if inv['status'] == 'paid':
                        p = inv['payload'].split('_')
                        uid, mode, months = int(p[1]), p[2], int(p[3])
                        add_subscription(uid, mode, months, float(inv['amount']))
                        conn = sqlite3.connect('users.db'); c = conn.cursor()
                        c.execute('UPDATE invoices SET status=\'paid\' WHERE invoice_id=?', (str(iid),))
                        conn.commit(); conn.close()
                        return True, uid, mode, months
    except: pass
    return False, None, None, None

# ============================================
# 🧠 ДВИЖОК
# ============================================
class SniperEngine:
    def calc_adx(self, kl, p=14):
        if len(kl)<p+1: return 0
        h=[k['high'] for k in kl]; l=[k['low'] for k in kl]; c=[k['close'] for k in kl]
        tr,pdm,mdm=[],[],[]
        for i in range(1,len(kl)):
            tr.append(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
            u,d=h[i]-h[i-1],l[i-1]-l[i]
            pdm.append(u if u>d and u>0 else 0); mdm.append(d if d>u and d>0 else 0)
        if len(tr)<p: return 0
        atr=sum(tr[:p])/p; av=[atr]
        for i in range(p,len(tr)): atr=(atr*(p-1)+tr[i])/p; av.append(atr)
        ca=av[-1] if av else 0
        if ca==0: return 0
        sp=sum(pdm[:p])/p/ca*100; sm=sum(mdm[:p])/p/ca*100; ax=[]
        for i in range(p,len(tr)):
            idx=i-len(tr)+len(av)
            if idx<0 or idx>=len(av): continue
            sp=(sp*(p-1)+(pdm[i]/av[idx]*100))/p; sm=(sm*(p-1)+(mdm[i]/av[idx]*100))/p
            if sp+sm>0: ax.append(abs(sp-sm)/(sp+sm)*100)
        return sum(ax[-5:])/min(5,len(ax)) if ax else 0
    
    def calc_signal(self, sym, kl, cfg):
        if not kl or len(kl)<25: return None
        ep=kl[-1]['close']; vols=[k['volume'] for k in kl]
        o=[k['open'] for k in kl]; h=[k['high'] for k in kl]; l=[k['low'] for k in kl]; c=[k['close'] for k in kl]
        lb,sens,wr=cfg['SWEEP_LOOKBACK'],cfg['SWEEP_SENSITIVITY'],cfg['SWEEP_WICK_RATIO']
        rh=h[-lb:]; rl=l[-lb:]
        eqh,eqhc=None,0; eql,eqlc=None,0
        for i in range(len(rh)):
            cl=1
            for j in range(i+1,len(rh)):
                if abs(rh[i]-rh[j])/ep<sens: cl+=1
            if cl>eqhc: eqhc,eqh=cl,rh[i]
        for i in range(len(rl)):
            cl=1
            for j in range(i+1,len(rl)):
                if abs(rl[i]-rl[j])/ep<sens: cl+=1
            if cl>eqlc: eqlc,eql=cl,rl[i]
        last=kl[-1]; lbd=abs(last['close']-last['open'])
        uw=last['high']-max(last['close'],last['open']); lw=min(last['close'],last['open'])-last['low']
        swSig,swStr='NEUTRAL',0
        if eqh and eqhc>=2 and last['high']>eqh*(1-sens) and last['close']<eqh: swSig,swStr='SHORT',eqhc*(2 if uw>lbd*wr else 1)
        if eql and eqlc>=2 and last['low']<eql*(1+sens) and last['close']>eql: swSig,swStr='LONG',eqlc*(2 if lw>lbd*wr else 1)
        gs,plb=cfg['POC_GRID_SIZE'],cfg['POC_LOOKBACK']; vp={}
        for i in range(len(kl)-plb,len(kl)):
            b=round(c[i]/(ep*gs))*(ep*gs); vp[b]=vp.get(b,0)+vols[i]
        poc,mv,tv=ep,0,0
        for pr,vl in vp.items(): tv+=vl
        if tv>0:
            for pr,vl in vp.items():
                if vl>mv: mv=vl; poc=float(pr)
        pocRel=tv>0 and (mv/tv*100>12)
        dlb,ww,dr=cfg['DELTA_LOOKBACK'],cfg['DELTA_WICK_WEIGHT'],cfg['DELTA_CONFIRMATION_RATIO']
        bp,sp=0,0; si=max(0,len(kl)-dlb)
        for i in range(si,len(kl)):
            bd=abs(c[i]-o[i]); uw2=h[i]-max(c[i],o[i]); lw2=min(c[i],o[i])-l[i]
            rw=1+(i-si)/dlb
            if c[i]>o[i]: bp+=(bd+lw2*ww)*rw; sp+=uw2*ww*rw
            else: sp+=(bd+uw2*ww)*rw; bp+=lw2*ww*rw
        de='LONG' if bp>sp*dr else ('SHORT' if sp>bp*dr else 'NEUTRAL')
        trv=[]
        for i in range(max(1,len(kl)-20),len(kl)):
            pc=c[i-1] if i>0 else c[i]
            trv.append(max(h[i]-l[i],abs(h[i]-pc),abs(l[i]-pc)))
        atr=sum(trv)/len(trv) if trv else 0
        adx=self.calc_adx(kl)
        uv,dv=0,0; pw=4 if pocRel else 1; pz=cfg['POC_CONFIRMATION_ZONE']
        if ep<poc*(1-pz): uv+=pw
        elif ep>poc*(1+pz): dv+=pw
        if swSig=='LONG': uv+=7+swStr
        elif swSig=='SHORT': dv+=7+swStr
        if de=='LONG': uv+=4
        elif de=='SHORT': dv+=4
        is_tr=adx>=cfg['ADX_THRESHOLD']
        if not is_tr: uv*=0.5; dv*=0.5
        tv=uv+dv; up=(uv/tv*100) if tv>0 else 50
        trend,conf='NEUTRAL',50
        if up>=cfg['MIN_AGREEMENT'] and uv>=5: trend,conf='UP',round(up*0.95)
        elif up<=(100-cfg['MIN_AGREEMENT']) and dv>=5: trend,conf='DOWN',round((100-up)*0.95)
        all3=swSig==trend and ((trend=='UP' and de=='LONG') or (trend=='DOWN' and de=='SHORT'))
        if not all3 and trend!='NEUTRAL':
            conf=round(conf*0.6)
            if conf<cfg['MIN_AGREEMENT']: trend,conf='NEUTRAL',round(conf*0.5)
        sig='NEUTRAL'
        if trend=='UP' and ep<poc and swSig=='LONG' and de=='LONG': sig,conf='LONG',min(100,conf+10)
        elif trend=='DOWN' and ep>poc and swSig=='SHORT' and de=='SHORT': sig,conf='SHORT',min(100,conf+10)
        elif trend=='UP' and ep<poc: conf=round(conf*0.3)
        elif trend=='DOWN' and ep>poc: conf=round(conf*0.3)
        if sig!='NEUTRAL':
            av=sum(vols[-10:-1])/9 if len(vols)>=10 else 0
            if vols[-1]<=av*1.3:
                conf=round(conf*0.7)
                if conf<cfg['MIN_AGREEMENT']: sig='NEUTRAL'
        if abs(ep-poc)/ep<pz: sig,conf='NEUTRAL',round(conf*0.2)
        if swSig=='NEUTRAL' and de=='NEUTRAL': sig,conf='NEUTRAL',round(conf*0.5)
        vp=(atr/ep*100) if ep>0 else 0
        if vp>10: sm,tm=2.0,4.0
        elif vp>5: sm,tm=1.8,3.6
        elif vp>2: sm,tm=1.5,3.0
        else: sm,tm=1.2,2.4
        if sig=='LONG':
            ssl=eql*0.997 if eql else ep-atr; sl=min(ssl,ep-sm*atr); tp=poc
            if eqh and eqh>poc: tp=(poc+eqh)/2
        elif sig=='SHORT':
            ssl=eqh*1.003 if eqh else ep+atr; sl=max(ssl,ep+sm*atr); tp=poc
            if eql and eql<poc: tp=(poc+eql)/2
        else: sl,tp=ep-atr,ep+atr
        return {'symbol':sym,'signal':sig,'confidence':min(100,max(0,conf)),'entry':round(ep,2),'sl':round(sl,2),'tp':round(tp,2),'poc':round(poc,2),'adx':round(adx,1),'sweep_signal':swSig,'delta':de,'up_percent':round(up,0),'all_three_agree':all3,'is_strong_trend':is_tr}


class SniperBot:
    def __init__(self, cfg):
        self.cfg = cfg; self.eng = SniperEngine(); self.app = None
        self.mon = False; self.la = {}; self.st = {'s':0,'l':0,'sh':0,'start':datetime.now()}
        self.EM = {"BTCUSDT":"₿","ETHUSDT":"Ξ","BNBUSDT":"🟡","SOLUSDT":"◎","XRPUSDT":"💧","DOGEUSDT":"🐶","ADAUSDT":"🔷","AVAXUSDT":"🔺","DOTUSDT":"⚪","LINKUSDT":"🔗"}
    
    def get_uid(self, update):
        if hasattr(update, 'effective_user'): return update.effective_user.id
        if hasattr(update, 'from_user'): return update.from_user.id
        return 0
    
    async def start_bot(self):
        self.app = Application.builder().token(self.cfg['TELEGRAM_TOKEN']).build()
        for cmd, fn in [('start',self.start),('scan',self.scan),('monitor',self.monitor_cmd),('stop',self.stop),('status',self.status),('stats',self.stats_cmd),('profile',self.profile),('mode',self.mode_cmd),('admin',self.admin),('add',self.add_sub)]:
            self.app.add_handler(CommandHandler(cmd, fn))
        self.app.add_handler(CallbackQueryHandler(self.callback))
        logger.info("Bot started!"); await self.app.initialize(); await self.app.start()
        await self.app.updater.start_polling(); logger.info("OK!")
    
    async def show_welcome(self, update):
        kb = [[InlineKeyboardButton(f"{v['emoji']} {v['name']}", callback_data=f'mode_{k}')] for k, v in MODES.items()]
        await update.message.reply_text(
            "Привет, трейдер! 🎯\n\n"
            "Я — CryptoSignals Pro, твой персональный аналитик рынка.\n\n"
            "Мои алгоритмы ищут входы по стратегии Smart Money на основе трёх китов:\n"
            "🐋 Свипы ликвидности\n"
            "🐋 Объёмный профиль (POC)\n"
            "🐋 Дельта-анализ\n\n"
            "У меня есть три режима работы. Выбери тот, что подходит тебе:\n\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "🔵 ПУЛЕМЁТ\n10-20 сигналов/день | Точность ~60%\nДля скальперов и активного трейдинга\nПробный: 7 дней бесплатно\n\n"
            "🟡 ОХОТНИК\n3-8 сигналов/день | Точность ~75%\nДля дей-трейдеров и регулярных сделок\nПробный: 14 дней бесплатно\n\n"
            "🟠 СНАЙПЕР\n0-3 сигнала/день | Точность ~95%\nДля свинг-трейдинга и крупных позиций\nПробный: 30 дней бесплатно\n\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "💰 ТАРИФЫ (для любого режима):\n"
            "▫️ 1 мес — 49.99 USDT\n▫️ 3 мес — 129.99 USDT\n"
            "▫️ 6 мес — 249.99 USDT\n▫️ 1 год — 499.99 USDT\n\n"
            "🎁 В пробный период — сигналы от ВСЕХ трёх режимов с пометкой режима!\n\n"
            "Что ты получаешь в любом режиме:\n"
            "✅ Точные уровни входа, TP и SL\n✅ Поддержка в Telegram\n"
            "✅ Автоматическая рассылка сигналов\n✅ Можно оплатить несколько режимов сразу\n\n"
            "Выбери свой режим:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML'
        )
    
    async def show_mode_card(self, update, mode):
        m = MODES[mode]; p = self.cfg['PRICES']
        desc = {"machinegun":"Самый быстрый режим!","hunter":"Золотая середина!","sniper":"Только лучшие входы!"}
        detail = {"machinegun":"Для тех, кто любит экшн и не боится риска.","hunter":"Для тех, кто хочет баланс между количеством и качеством.","sniper":"Для тех, кто ценит качество выше количества."}
        tip = {"machinegun":"💡 Идеально для внутридневной торговли","hunter":"💡 Идеально для регулярного трейдинга","sniper":"💡 Идеально для крупных позиций и долгих сделок"}
        kb = [
            [InlineKeyboardButton(f"🎁 {m['trial_days']} дней бесплатно", callback_data=f'trial_{mode}')],
            [InlineKeyboardButton(f"💳 1 мес — {p['1']} USDT", callback_data=f'buy_{mode}_1')],
            [InlineKeyboardButton(f"💳 3 мес — {p['3']} USDT", callback_data=f'buy_{mode}_3')],
            [InlineKeyboardButton(f"💳 6 мес — {p['6']} USDT", callback_data=f'buy_{mode}_6')],
            [InlineKeyboardButton(f"💳 1 год — {p['12']} USDT", callback_data=f'buy_{mode}_12')],
            [InlineKeyboardButton("⬅️ Назад", callback_data='back_to_welcome')]
        ]
        await update.message.reply_text(
            f"{m['emoji']} {m['name'].upper()}\n\n"
            f"{desc[mode]}\n{m['signal_range']} сигналов/день.\n{detail[mode]}\n\n"
            f"🎯 Точность: ~{m['accuracy']}\n⏱ Таймфрейм: 1H\n👥 Для {m['for_who']}\n\n"
            "Что ты получаешь:\n"
            f"✅ {m['signal_range']} сигналов ежедневно\n"
            "✅ Точные уровни входа, TP и SL\n"
            "✅ Автоматическая рассылка в Telegram\n✅ Поддержка\n\n"
            f"{tip[mode]}\n\nГотов попробовать?",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML'
        )
    
    async def show_main_menu(self, update, uid):
        active = get_active_modes(uid) if str(uid) != self.cfg['ADMIN_ID'] else list(MODES.keys())
        is_admin = str(uid) == self.cfg['ADMIN_ID']
        mode_names = ', '.join([f"{MODES[m]['emoji']} {MODES[m]['name']}" for m in active]) if active else 'нет'
        kb = [
            [InlineKeyboardButton("🔍 Сканировать", callback_data='scan')],
            [InlineKeyboardButton("🔄 Сменить режим", callback_data='mode_select')],
            [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
            [InlineKeyboardButton("👤 Профиль", callback_data='profile')],
        ]
        kb.append([InlineKeyboardButton("👑 Админ-панель", callback_data='admin')] if is_admin else [InlineKeyboardButton("💳 Продлить подписку", callback_data='mode_select')])
        await update.message.reply_text(
            f"🎯 CryptoSignals Pro\n\n✅ Доступ активен\n🎯 Текущие режимы: {mode_names}\n\nВыберите действие:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML'
        )
    
    async def start(self, update, context):
        uid = self.get_uid(update)
        register_user(uid, update.effective_user.username or 'unknown')
        if str(uid) == self.cfg['ADMIN_ID'] or has_any_access(uid):
            await self.show_main_menu(update, uid)
        else:
            await self.show_welcome(update)
    
    async def mode_cmd(self, update, context):
        kb = [[InlineKeyboardButton(f"{v['emoji']} {v['name']}", callback_data=f'mode_{k}')] for k, v in MODES.items()]
        await update.message.reply_text("🎯 Выберите режим:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    
    async def scan(self, update, context):
        uid = self.get_uid(update)
        if str(uid) != self.cfg['ADMIN_ID'] and not has_any_access(uid):
            return await self.show_welcome(update)
        await update.message.reply_text("🔍 Сканирую...")
        results = await self.scan_all()
        if not results: await update.message.reply_text("😴 Нет сигналов.")
        else:
            for s in results[:5]: await update.message.reply_text(self.fmt_signal(s), parse_mode='HTML')
    
    async def monitor_cmd(self, update, context):
        if self.mon: await update.message.reply_text("⚠️ Уже запущен!")
        else: self.mon = True; await update.message.reply_text("🟢 Мониторинг запущен!"); asyncio.create_task(self.monitor_loop())
    
    async def stop(self, update, context): self.mon = False; await update.message.reply_text("🔴 Остановлен.")
    async def status(self, update, context):
        s = "🟢" if self.mon else "🔴"
        await update.message.reply_text(f"📊 Статус: {s}\n🎯 Сигналов: {self.st['s']}")
    async def stats_cmd(self, update, context):
        up = datetime.now() - self.st['start']
        await update.message.reply_text(f"📊 Сигналов: {self.st['s']}\n📈 ЛОНГ: {self.st['l']}\n📉 ШОРТ: {self.st['sh']}\n⏱ {up.days}д {up.seconds//3600}ч")
    
    async def profile(self, update, context):
        uid = self.get_uid(update)
        if str(uid) == self.cfg['ADMIN_ID']:
            txt = "👑 Админ — все режимы"
        else:
            active = get_active_modes(uid)
            txt = 'Активные режимы:\n' + '\n'.join([f"  {MODES[m]['emoji']} {MODES[m]['name']}" for m in active]) if active else "Нет активных подписок"
        await update.message.reply_text(f"👤 ID: <code>{uid}</code>\n{txt}", parse_mode='HTML')
    
    async def admin(self, update, context):
        if str(self.get_uid(update)) != self.cfg['ADMIN_ID']: return await update.message.reply_text("⛔")
        t, a, r, ms = get_admin_stats()
        lines = '\n'.join([f"{MODES[m]['emoji']} {MODES[m]['name']}: покупок {ms[m]['paid_count']}, месяцев {ms[m]['total_months']}, пробных {ms[m]['trials']}" for m in MODES])
        await update.message.reply_text(f"👑 АДМИН\n\n👥 Всего: {t} | Активных: {a}\n💰 Выручка: {r:.2f} USDT\n\n📊 По режимам:\n{lines}\n\n/add ID МЕС РЕЖИМ", parse_mode='HTML')
    
    async def add_sub(self, update, context):
        if str(self.get_uid(update)) != self.cfg['ADMIN_ID']: return await update.message.reply_text("⛔")
        try:
            uid = int(context.args[0]); months = int(context.args[1])
            mode = context.args[2] if len(context.args) > 2 else 'hunter'
            if mode not in MODES: mode = 'hunter'
            add_subscription(uid, mode, months)
            await update.message.reply_text(f"✅ {uid} — {MODES[mode]['name']} на {months} мес.")
        except: await update.message.reply_text("/add ID МЕС РЕЖИМ")
    
    async def callback(self, update, context):
        q = update.callback_query; await q.answer(); d = q.data
        uid = q.from_user.id
        logger.info(f"🔘 {d} | {uid}")
        try:
            if d.startswith('mode_'): await self.show_mode_card(q, d.split('_')[1]); return
            if d.startswith('trial_'):
                mode = d.split('_')[1]
                if mode in MODES:
                    start_trial(uid, mode)
                    await q.message.reply_text(f"🎉 Пробный период активирован!\n\n{MODES[mode]['emoji']} {MODES[mode]['name']}\n✅ {MODES[mode]['trial_days']} дней\n\n/scan — начать", parse_mode='HTML')
                return
            if d.startswith('buy_'):
                _, mode, months = d.split('_')
                if mode in MODES:
                    inv = await create_invoice(uid, int(months), mode)
                    if inv:
                        kb = [[InlineKeyboardButton("💳 Оплатить", url=inv['url']), InlineKeyboardButton("🔄 Проверить", callback_data=f'chk_{inv["id"]}')]]
                        await q.message.reply_text(f"💳 Счёт на {CONFIG['PRICES'][months]} USDT\n📦 {MODES[mode]['name']} — {months} мес.", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                    else: await q.message.reply_text("❌ Ошибка создания счёта.")
                return            if d.startswith('chk_'):
                ok, _, mode, months = await check_invoice(d.split('_')[1])
                await q.message.reply_text(f"✅ Оплачено! {MODES.get(mode,{}).get('name',mode)} — {months} мес." if ok else "⏳ Ещё нет", parse_mode='HTML')
                return
            if d == 'mode_select':
                kb = [[InlineKeyboardButton(f"{v['emoji']} {v['name']}", callback_data=f'mode_{k}')] for k, v in MODES.items()]
                await q.message.reply_text("🎯 Выберите режим:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                return
            if d == 'back_to_welcome': await self.show_welcome(q); return
            if d == 'scan': await self.scan(q, context); return
            if d == 'stats': await self.stats_cmd(q, context); return
            if d == 'profile': await self.profile(q, context); return
            if d == 'admin': await self.admin(q, context); return
        except Exception as e:
            logger.error(f"❌ {e}")
            await q.message.reply_text(f"❌ {e}")
    
    async def scan_all(self):
        results = []
        for sym in self.cfg['SYMBOLS']:
            try:
                kl = await self.fetch_kl(sym)
                if kl:
                    for mk, mc in MODES.items():
                        sig = self.eng.calc_signal(sym, kl, mc)
                        if sig and sig['signal'] != 'NEUTRAL' and sig['confidence'] >= 50:
                            sig['mode'] = mk; results.append(sig)
            except: pass
        results.sort(key=lambda x: x['confidence'], reverse=True)
        return results
    
    async def fetch_kl(self, sym):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.binance.com/api/v3/klines", params={'symbol':sym,'interval':'1h','limit':50}, proxy=PROXY) as r:
                    if r.status==200:
                        d = await r.json()
                        return [{'open':float(k[1]),'high':float(k[2]),'low':float(k[3]),'close':float(k[4]),'volume':float(k[5])} for k in d]
        except: return None
    
    def fmt_signal(self, s):
        em = self.EM.get(s['symbol'], '●')
        d = "🟢 ЛОНГ" if s['signal'] == 'LONG' else "🔴 ШОРТ"
        m = MODES.get(s.get('mode', ''), {})
        me = m.get('emoji', ''); mn = m.get('name', '')
        return (
            f"{me} {mn}: {em} <b>{s['symbol']}: {d}</b>\n"
            f"🎯 Уверенность: <b>{s['confidence']}%</b>\n\n"
            f"📊 Вход: <code>{s['entry']}</code>\n"
            f"🎯 TP: <code>{s['tp']}</code>\n"
            f"🛑 SL: <code>{s['sl']}</code>\n\n"
            f"📈 ADX: {s['adx']} | Согласие: {s['up_percent']}%\n"
            f"Все 3: {'🔥 ДА' if s['all_three_agree'] else '⚠️ НЕТ'} | {datetime.now().strftime('%H:%M')}"
        )
    
    async def monitor_loop(self):
        logger.info("Monitor started")
        while self.mon:
            try:
                sigs = await self.scan_all()
                for s in sigs:
                    key = f"{s['symbol']}_{s['signal']}_{s.get('mode','')}"
                    if key not in self.la or datetime.now()-self.la[key]>timedelta(hours=4):
                        self.la[key]=datetime.now()
                        mode = s.get('mode', '')
                        msg = self.fmt_signal(s)
                        conn = sqlite3.connect('users.db'); c = conn.cursor()
                        c.execute("SELECT DISTINCT user_id FROM subscriptions WHERE mode=? AND subscribed_until > ?",
                                  (mode, datetime.now().isoformat()))
                        users = c.fetchall()
                        users.append((int(self.cfg['ADMIN_ID']),))
                        conn.close()
                        sent = 0
                        for (uid,) in set(users):
                            try:
                                await self.app.bot.send_message(uid, msg, parse_mode='HTML')
                                sent += 1
                            except: pass
                        self.st['s']+=1
                        if s['signal']=='LONG': self.st['l']+=1
                        else: self.st['sh']+=1
                        try: await self.app.bot.send_message(self.cfg['ADMIN_ID'], f"📤 {mode}: {sent} пользователям", parse_mode='HTML')
                        except: pass
                await asyncio.sleep(self.cfg['CHECK_INTERVAL'])
            except Exception as e:
                logger.error(f"Loop: {e}"); await asyncio.sleep(60)

async def main():
    bot = SniperBot(CONFIG)
    try:
        await bot.start_bot()
        logger.info("Ready!")
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stop")
    finally:
        bot.mon = False
        if bot.app:
            await bot.app.stop()
            await bot.app.shutdown()

if __name__ == '__main__':
    asyncio.run(main())
