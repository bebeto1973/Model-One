import os
import time
import logging
import smtplib
import ssl
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ==========================================
# 1. CONFIGURAZIONE E CREDENZIALI
# ==========================================
load_dotenv()

API_KEY = os.environ.get('ALPACA_API_KEY_MODEL_ONE')
API_SECRET = os.environ.get('ALPACA_SECRET_KEY_MODEL_ONE')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD') 
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')

log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.WARNING, format=log_format,
                    handlers=[logging.FileHandler("barbell_bot.log", encoding='utf-8'), logging.StreamHandler()])
logger = logging.getLogger()

# ==========================================
# 2. MODULO AGGIORNAMENTO DATABASE (Via Alpaca)
# ==========================================
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

def aggiorna_database_fondamentali(filepath='Database_Quantitativo_Definitivo.xlsx'):
    logger.warning("🔄 Avvio aggiornamento Database tramite Alpaca...")
    try:
        df = pd.read_excel(filepath)
        tickers = df['Ticker'].unique().tolist()
        
        # Prevenzione errore: creiamo la colonna a zero per sicurezza
        if 'Prezzo_Attuale' not in df.columns:
            df['Prezzo_Attuale'] = 0.0

        # Inizializza il client dati di Alpaca usando le tue chiavi esistenti
        data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
        
        # Alpaca gestisce benissimo le richieste, ma dividiamo i 500 ticker in piccoli blocchi 
        # per evitare timeout di rete
        chunk_size = 100
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i:i + chunk_size]
            try:
                req = StockLatestTradeRequest(symbol_or_symbols=chunk)
                trades = data_client.get_stock_latest_trade(req)
                
                for ticker, trade in trades.items():
                    df.loc[df['Ticker'] == ticker, 'Prezzo_Attuale'] = float(trade.price)
            except Exception as e:
                logger.warning(f"⚠️ Errore download Alpaca per il blocco {chunk[0]}... : {e}")
        
        df.to_excel(filepath, index=False)
        logger.warning(f"✅ Database aggiornato con gli ultimi prezzi per {len(tickers)} ticker.")
        return df
    except Exception as e:
        logger.warning(f"❌ Errore aggiornamento database: {e}")
        return None

# ==========================================
# 3. MOTORI QUANTITATIVI (MASTER SCREENER)
# ==========================================
def calcola_motori_quantitativi(df_master):
    logger.warning("⚙️ Calcolo Motori Quantitativi...")
    data_limite = pd.Timestamp('today') - pd.DateOffset(months=6)
    
    # Raggruppamento per prendere solo l'ultimo dato disponibile per ogni Ticker
    df_live = df_master.sort_values(by=['Ticker', 'Data']).groupby('Ticker').tail(1).copy()
    df_live['filingDate'] = pd.to_datetime(df_live.get('filingDate', df_live['Data']))
    df_live = df_live[df_live['filingDate'] >= data_limite]

    # Sicurezza: evito divisioni per zero
    azioni = df_live['Azioni Medie Base'].replace(0, np.nan)
    ricavi = df_live['Ricavi Totali (Discreto)'].replace(0, np.nan)
    market_cap = df_live['Prezzo_Attuale'] * azioni
    
    risultati = []

    # -- TECH (Rule of 40 & Gross Margin) --
    tech = df_live[df_live['Settore'] == 'Information Technology'].copy()
    if not tech.empty:
        sbc = tech.get('Stock-Based Compensation (SBC) (Discreto)', 0).fillna(0)
        fcf_rett = tech['Free Cash Flow YTD (Discreto)'] - sbc
        tech['Rule of 40'] = tech.get('Crescita YoY (%)', 0) + ((fcf_rett / ricavi) * 100)
        tech['Gross Margin (%)'] = (tech.get('Utile Lordo (Discreto)', 0) / ricavi) * 100
        tech = tech.dropna(subset=['Rule of 40'])
        tech['Score'] = ((tech['Rule of 40'].rank(pct=True) * 0.7) + (tech['Gross Margin (%)'].rank(pct=True) * 0.3)) * 100
        tech['Motore'] = 'Tech'
        risultati.append(tech)

    # -- DEFENSIVES (Banks, Ind, Util) --
    # Semplificato per spazio: applichiamo logiche simili mappando i settori
    defensives = df_live[~df_live['Settore'].isin(['Information Technology'])].copy()
    if not defensives.empty:
        # Usiamo un proxy generale per i difensivi basato su FCF Yield e ROE
        defensives['FCF Yield'] = (defensives['Free Cash Flow YTD (Discreto)'] / market_cap) * 100
        defensives['ROE'] = (defensives['Utile Netto (Discreto)'] / defensives['Patrimonio Netto'].replace(0, np.nan)) * 100
        defensives = defensives.dropna(subset=['FCF Yield', 'ROE'])
        defensives['Score'] = ((defensives['FCF Yield'].rank(pct=True) * 0.5) + (defensives['ROE'].rank(pct=True) * 0.5)) * 100
        defensives['Motore'] = 'Defensive'
        risultati.append(defensives)

    df_full = pd.concat(risultati, ignore_index=True)
    return df_full

# ==========================================
# 4. ALLOCATORE BARBELL (LONG/SHORT)
# ==========================================
def crea_portafoglio_barbell(df_scored):
    logger.warning("⚖️ Creazione Asset Allocation Barbell...")
    
    df_tech = df_scored[df_scored['Motore'] == 'Tech'].sort_values(by='Score', ascending=False).head(5)
    df_def = df_scored[df_scored['Motore'] == 'Defensive'].sort_values(by='Score', ascending=False).head(5)
    
    # Gamba LONG (10% cad)
    df_long = pd.concat([df_tech, df_def]).reset_index(drop=True)
    df_long['Posizione'] = 'LONG'
    df_long['Peso (%)'] = 10.0
    
    # Gamba SHORT (Hedge -50% cad)
    hedge_data = [
        {'Ticker': 'QQQ', 'Motore': 'Hedge', 'Posizione': 'SHORT', 'Score': 0, 'Peso (%)': 50.0},
        {'Ticker': 'SPY', 'Motore': 'Hedge', 'Posizione': 'SHORT', 'Score': 0, 'Peso (%)': 50.0}
    ]
    df_short = pd.DataFrame(hedge_data)
    
    df_portfolio = pd.concat([df_long, df_short], ignore_index=True)[['Ticker', 'Motore', 'Posizione', 'Score', 'Peso (%)']]
    return df_portfolio

# ==========================================
# 5. ESECUZIONE ALPACA (LIQUIDATE & REINVEST)
# ==========================================
def esegui_su_alpaca(df_portfolio):
    logger.warning("🤖 Connessione ad Alpaca in corso...")
    client = TradingClient(API_KEY, API_SECRET, paper=True)
    
    logger.warning("🧹 Chiusura ordini e posizioni precedenti...")
    client.cancel_orders()
    client.close_all_positions(cancel_orders=True)
    time.sleep(8) 
    
    account = client.get_account()
    equity = float(account.equity)
    logger.warning(f"💰 Equity Disponibile: ${equity:,.2f}")
    
    html_ordini = []
    
    for _, row in df_portfolio.iterrows():
        ticker, pos, peso = row['Ticker'], row['Posizione'], row['Peso (%)']
        importo_dollari = (peso / 100) * equity
        
        try:
            if pos == 'LONG':
                order = MarketOrderRequest(symbol=ticker, notional=round(importo_dollari, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                client.submit_order(order)
                html_ordini.append(f"<li>🟢 <b>LONG {ticker}</b>: ${importo_dollari:,.2f}</li>")
            elif pos == 'SHORT':
                prezzo = float(client.get_latest_trade(ticker).price)
                quote_intere = int(importo_dollari // prezzo)
                if quote_intere > 0:
                    order = MarketOrderRequest(symbol=ticker, qty=quote_intere, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                    client.submit_order(order)
                    html_ordini.append(f"<li>🔴 <b>SHORT {ticker}</b>: {quote_intere} quote (~${quote_intere*prezzo:,.2f})</li>")
        except Exception as e:
            logger.warning(f"❌ Errore ordine {ticker}: {e}")
            
    return equity, float(account.last_equity), html_ordini

# ==========================================
# 6. REPORTISTICA EMAIL
# ==========================================
def invia_report(equity_oggi, equity_ieri, ordini_html):
    if not SENDER_EMAIL: return
    pnl_usd = equity_oggi - equity_ieri
    pnl_pct = (pnl_usd / equity_ieri) * 100 if equity_ieri > 0 else 0
    colore = "#27ae60" if pnl_usd >= 0 else "#c0392b"
    
    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="background-color: #2c3e50; color: white; padding: 10px;">📊 Barbell Quant Fund - Daily Report</h2>
        <h3>Stato Conto Alpaca (1M Model)</h3>
        <p><b>Equity Totale:</b> ${equity_oggi:,.2f}</p>
        <p><b>PnL Odierno:</b> <span style="color: {colore}; font-weight: bold;">${pnl_usd:,.2f} ({pnl_pct:+.2f}%)</span></p>
        <hr>
        <h3>Esecuzione Allocazione (Liquidate & Reinvest)</h3>
        <ul>{''.join(ordini_html)}</ul>
    </body>
    </html>
    """
    msg = MIMEMultipart()
    msg['From'], msg['To'], msg['Subject'] = SENDER_EMAIL, RECEIVER_EMAIL, f"Report Barbell - PnL: {pnl_pct:+.2f}%"
    msg.attach(MIMEText(body, 'html', 'utf-8'))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        logger.warning("📧 Report email inviato.")
    except Exception as e:
        logger.warning(f"❌ Errore Email: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    df_aggiornato = aggiorna_database_fondamentali()
    if df_aggiornato is not None:
        df_scored = calcola_motori_quantitativi(df_aggiornato)
        df_portafoglio = crea_portafoglio_barbell(df_scored)
        eq_oggi, eq_ieri, ordini = esegui_su_alpaca(df_portafoglio)
        invia_report(eq_oggi, eq_ieri, ordini)
