import os
import threading
import uuid
import logging
import html
from datetime import datetime, timedelta
from flask import Flask
import psycopg2
from psycopg2 import pool
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, filters, MessageHandler
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configuración de Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = 5005408123
PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN or not DATABASE_URL:
    logger.error("Error: TELEGRAM_TOKEN o DATABASE_URL no están configurados.")
    exit(1)

# --- BASE DE DATOS ---
try:
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)
    if db_pool:
        logger.info("Conexión a la base de datos establecida correctamente.")
except Exception as e:
    logger.error(f"Error al conectar con la base de datos: {e}")
    exit(1)

def init_db():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS licencias (
                    id SERIAL PRIMARY KEY,
                    clave_licencia TEXT UNIQUE NOT NULL,
                    duracion_tipo TEXT NOT NULL,
                    dias_totales INTEGER NOT NULL,
                    fecha_creacion TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    fecha_expiracion TIMESTAMP WITH TIME ZONE,
                    estado TEXT DEFAULT 'activa',
                    cliente_nota TEXT
                );
            """)
            conn.commit()
            logger.info("Tabla 'licencias' verificada/creada con éxito.")
    except Exception as e:
        logger.error(f"Error inicializando la base de datos: {e}")
    finally:
        db_pool.putconn(conn)

# --- FLASK (Para Render) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot de Gestión de Licencias está activo.", 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# --- DECORADOR DE SEGURIDAD ---
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            logger.warning(f"Intento de acceso no autorizado por ID: {user_id}")
            if update.message:
                await update.message.reply_text("No tienes acceso a este bot.")
            return
        
        # Verificar si está autenticado con la clave 8040
        if not context.user_data.get('is_auth'):
            # Permitir el comando start y la función de verificar clave
            if update.message and update.message.text:
                if update.message.text.startswith('/start') or func.__name__ == 'verificar_clave':
                    return await func(update, context)
            
            if update.message:
                await update.message.reply_text("🔒 Por favor, introduce la clave de acceso para continuar.")
            return
            
        return await func(update, context)
    return wrapper

# --- COMANDOS DEL BOT ---

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('is_auth'):
        await mostrar_menu(update)
    else:
        await update.message.reply_text("🛡️ <b>Sistema de Seguridad</b>\n\nPor favor, introduce la clave de acceso:", parse_mode='HTML')

async def mostrar_menu(update: Update):
    help_text = (
        "👋 <b>Bienvenido al Gestor de Licencias</b>\n\n"
        "Comandos disponibles:\n"
        "• /generar [tipo/dias] [nota] - Generar nueva licencia\n"
        "  Ej: <code>/generar 7 Juan</code> o <code>/generar vitalicia Pedro</code>\n"
        "• /status [clave] - Ver estado de una licencia\n"
        "• /vertodas - Ver todas las licencias registradas\n"
        "• /activar [clave] - Activar licencia\n"
        "• /desactivar [clave] - Suspender licencia\n"
        "• /ayuda - Mostrar este panel"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

@admin_only
async def verificar_clave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "8040":
        context.user_data['is_auth'] = True
        await update.message.reply_text("✅ <b>Acceso Concedido</b>", parse_mode='HTML')
        await mostrar_menu(update)
    elif not context.user_data.get('is_auth'):
        await update.message.reply_text("❌ Clave incorrecta. Inténtalo de nuevo.")

@admin_only
async def vertodas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT clave_licencia, duracion_tipo, estado, cliente_nota, fecha_expiracion FROM licencias ORDER BY id DESC")
            rows = cur.fetchall()
            if not rows:
                await update.message.reply_text("No hay licencias registradas.")
                return

            mensaje = "📋 <b>Lista de Todas las Licencias</b>\n\n"
            for row in rows:
                c, t, s, n, e = row
                # Formatear fecha y hora
                if e:
                    exp_str = e.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    exp_str = 'Nunca'
                
                if s == 'activa':
                    estado_emoji = "✅"
                elif s == 'expirada':
                    estado_emoji = "⚠️"
                else:
                    estado_emoji = "🚫"

                mensaje += (
                    f"{estado_emoji} <b>Clave:</b> <code>{html.escape(c)}</code>\n"
                    f"👤 <b>Cliente:</b> {html.escape(n if n else 'N/A')}\n"
                    f"⏳ <b>Expira:</b> {exp_str}\n"
                    f"⚙️ <b>Estado:</b> {html.escape(s.upper())}\n"
                    "----------------------------\n"
                )
                
                # Telegram tiene un límite de 4096 caracteres por mensaje
                if len(mensaje) > 3500:
                    await update.message.reply_text(mensaje, parse_mode='HTML')
                    mensaje = ""
            
            if mensaje:
                await update.message.reply_text(mensaje, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error en vertodas: {e}")
        await update.message.reply_text("Error al obtener las licencias.")
    finally:
        db_pool.putconn(conn)

# --- TAREA DE SEGUNDO PLANO: VERIFICAR EXPIRACIONES ---
async def check_expirations_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando chequeo automático de expiraciones...")
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            # Buscar licencias activas que ya pasaron su fecha de expiración
            cur.execute("""
                UPDATE licencias 
                SET estado = 'expirada' 
                WHERE estado = 'activa' 
                AND fecha_expiracion IS NOT NULL 
                AND fecha_expiracion <= NOW()
                RETURNING clave_licencia, cliente_nota;
            """)
            expired_licenses = cur.fetchall()
            conn.commit()
            
            for clave, nota in expired_licenses:
                msg = (
                    "⚠️ <b>LICENCIA CADUCADA</b>\n\n"
                    f"La licencia <code>{html.escape(clave)}</code> ha expirado.\n"
                    f"👤 <b>Cliente:</b> {html.escape(nota if nota else 'N/A')}\n"
                    f"📅 <b>Estado:</b> Marcada como EXPIRADA automáticamente."
                )
                try:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode='HTML')
                    logger.info(f"Notificación enviada para licencia {clave}")
                except Exception as send_err:
                    logger.error(f"No se pudo enviar notificación a {ADMIN_ID}: {send_err}")
                    
    except Exception as e:
        logger.error(f"Error en el proceso de verificación de expiraciones: {e}")
    finally:
        db_pool.putconn(conn)

@admin_only
async def generar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /generar [7_dias/15_dias/vitalicia/N_dias] [nota_cliente]")
        return

    tipo_input = context.args[0].lower()
    nota_cliente = " ".join(context.args[1:])
    
    dias = 0
    duracion_tipo = tipo_input
    fecha_expiracion = None

    if tipo_input == '7_dias' or tipo_input == '7':
        dias = 7
        duracion_tipo = '7_dias'
    elif tipo_input == '15_dias' or tipo_input == '15':
        dias = 15
        duracion_tipo = '15_dias'
    elif tipo_input == 'vitalicia':
        dias = 99999 # Representación interna
        duracion_tipo = 'vitalicia'
    else:
        try:
            dias = int(tipo_input)
            duracion_tipo = 'personalizado'
        except ValueError:
            await update.message.reply_text("Tipo de duración no válido. Usa un número o 'vitalicia'.")
            return

    if duracion_tipo != 'vitalicia':
        fecha_expiracion = datetime.now() + timedelta(days=dias)

    clave = str(uuid.uuid4()).replace('-', '').upper()[:12]
    
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO licencias (clave_licencia, duracion_tipo, dias_totales, fecha_expiracion, cliente_nota)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING clave_licencia;
            """, (clave, duracion_tipo, dias, fecha_expiracion, nota_cliente))
            conn.commit()
            
            res_text = (
                "✅ <b>Licencia Generada</b>\n\n"
                f"🔑 <b>Clave:</b> <code>{html.escape(clave)}</code>\n"
                f"📅 <b>Duración:</b> {html.escape(duracion_tipo)} ({dias} días)\n"
                f"👤 <b>Cliente:</b> {html.escape(nota_cliente)}\n"
                f"⏳ <b>Expira:</b> {fecha_expiracion.strftime('%Y-%m-%d') if fecha_expiracion else 'Nunca'}"
            )
            await update.message.reply_text(res_text, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error al generar licencia: {e}")
        await update.message.reply_text("Error interno al generar la licencia.")
    finally:
        db_pool.putconn(conn)

@admin_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /status [clave]")
        return
    
    clave = context.args[0].upper()
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT clave_licencia, duracion_tipo, estado, cliente_nota, fecha_expiracion FROM licencias WHERE clave_licencia = %s", (clave,))
            row = cur.fetchone()
            if row:
                c, t, s, n, e = row
                exp_str = e.strftime('%Y-%m-%d') if e else 'Nunca'
                await update.message.reply_text(
                    f"📊 <b>Estado de Licencia</b>\n\n"
                    f"🔑 Clave: <code>{html.escape(c)}</code>\n"
                    f"📝 Nota: {html.escape(n if n else 'N/A')}\n"
                    f"⚙️ Estado: {html.escape(s.upper())}\n"
                    f"⏳ Expira: {exp_str}",
                    parse_mode='HTML'
                )
            else:
                await update.message.reply_text("❌ Licencia no encontrada.")
    except Exception as e:
        logger.error(f"Error en status: {e}")
    finally:
        db_pool.putconn(conn)

async def set_estado(update: Update, context: ContextTypes.DEFAULT_TYPE, nuevo_estado: str):
    if not context.args:
        await update.message.reply_text(f"Uso: /{context.command} [clave]")
        return
    
    clave = context.args[0].upper()
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE licencias SET estado = %s WHERE clave_licencia = %s RETURNING clave_licencia", (nuevo_estado, clave))
            row = cur.fetchone()
            if row:
                conn.commit()
                await update.message.reply_text(f"✅ Licencia `{clave}` marcada como {nuevo_estado}.")
            else:
                await update.message.reply_text("❌ Licencia no encontrada.")
    except Exception as e:
        logger.error(f"Error al cambiar estado: {e}")
    finally:
        db_pool.putconn(conn)

@admin_only
async def activar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_estado(update, context, 'activa')

@admin_only
async def desactivar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_estado(update, context, 'suspendida')

# --- MAIN ---
if __name__ == '__main__':
    # Inicializar DB
    init_db()

    # Iniciar Flask en un hilo secundario
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info(f"Servidor Flask iniciado en el puerto {PORT}")

    # Iniciar Bot de Telegram
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Programar chequeo de expiraciones cada 30 minutos (1800 segundos)
    if application.job_queue:
        application.job_queue.run_repeating(check_expirations_job, interval=1800, first=10)
        logger.info("Tarea programada: Verificación de expiraciones cada 30 min.")
    
    # Handlers
    application.add_handler(CommandHandler(["start", "ayuda"], start))
    application.add_handler(CommandHandler("generar", generar))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("vertodas", vertodas))
    application.add_handler(CommandHandler("activar", activar))
    application.add_handler(CommandHandler("desactivar", desactivar))
    
    # Handler para la clave de acceso (mensajes de texto que no son comandos)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, verificar_clave))

    logger.info("Bot de Telegram iniciado (Polling)...")
    application.run_polling()
