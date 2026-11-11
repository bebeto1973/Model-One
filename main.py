import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Credenziali
API_KEY = os.environ.get('ALPACA_API_KEY_MODEL_ONE', '').strip().replace('"', '').replace("'", "")
API_SECRET = os.environ.get('ALPACA_SECRET_KEY_MODEL_ONE', '').strip().replace('"', '').replace("'", "")
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', '').strip().replace('"', '').replace("'", "")
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD', '').strip().replace('"', '').replace("'", "")
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL', '').strip().replace('"', '').replace("'", "")

if len(API_KEY) == 0:
    logger.error("ERRORE: API Key mancante nei Secrets.")
    
trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

def calcola_statistiche_performance():
    logger.info("📊 Calcolo metriche di performance da Alpaca...")
    try:
        account = trading_client.get_account()
        
        equity_attuale = float(account.equity) if account.equity else 0.0
        equity_ieri = float(account.last_equity) if account.last_equity else equity_attuale
        cash_iniziale = float(account.initial_margin) if getattr(account, 'initial_margin', None) else 100000.0 
        
        daily_pnl_perc = ((equity_attuale - equity_ieri) / equity_ieri) * 100 if equity_ieri > 0 else 0.0
        inception_pnl_perc = ((equity_attuale - cash_iniziale) / cash_iniziale) * 100 if cash_iniziale > 0 else 0.0
        
        hwm = float(account.highest_equity) if getattr(account, 'highest_equity', None) else equity_attuale
        max_drawdown = ((equity_attuale - hwm) / hwm) * 100 if hwm > 0 else 0.0

        stats = {
            "Metrica": ["Equity Attuale", "PnL Giornaliero", "PnL Complessivo", "Drawdown Attuale"],
            "Valore": [
                f"${equity_attuale:,.2f}", 
                f"{daily_pnl_perc:+.2f}%", 
                f"{inception_pnl_perc:+.2f}%", 
                f"{max_drawdown:.2f}%"
            ]
        }
        return pd.DataFrame(stats)
    except Exception as e:
        logger.warning(f"⚠️ Errore calcolo performance: {e}")
        return None

def esegui_rebalancing_market_neutral():
    logger.info("🤖 Avvio Esecutore Market-Neutral...")
    ordini_eseguiti = []
    
    try:
        if not os.path.exists('Top_15_Live.xlsx'):
            logger.error("❌ File Top_15_Live.xlsx non trovato!")
            return None, None
            
        df_top15 = pd.read_excel('Top_15_Live.xlsx')
        tickers_alpaca = [str(t).replace('-', '.') for t in df_top15['Ticker'].tolist()] if not df_top15.empty else []
        
        # BUG RISOLTO: Rimosso il return None che spegneva il bot in Cash Mode.
        if len(tickers_alpaca) == 0:
            logger.warning("⚠️ Modalità CASH (100% Liquido). Smontaggio portafoglio in corso.")
        
        account = trading_client.get_account()
        equity = float(account.equity)
        
        # Se 0 titoli, target 0 per tutti e 0% di Hedge (Market Neutral = No Long, No Short)
        target_importo_long_singolo = (equity / len(tickers_alpaca)) if len(tickers_alpaca) > 0 else 0.0
        target_importo_short_spy = equity * 1.0 if len(tickers_alpaca) > 0 else 0.0 
        
        tutti_i_ticker = tickers_alpaca + ['SPY']
        prezzi_live = {}
        
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=tutti_i_ticker)
            trades = data_client.get_stock_latest_trade(req)
            prezzi_live = {sym: float(trade.price) for sym, trade in trades.items() if trade and hasattr(trade, 'price')}
        except Exception as bulk_e:
            for tk in tutti_i_ticker:
                try:
                    r = StockLatestTradeRequest(symbol_or_symbols=[tk])
                    t = data_client.get_stock_latest_trade(r)
                    if tk in t: prezzi_live[tk] = float(t[tk].price)
                except: pass
                
        posizioni_esistenti = {p.symbol: {'qty': float(p.qty), 'market_value': float(p.market_value)} for p in trading_client.get_all_positions()}
            
        # 1. CHIUSURA DISMESSE (o CHIUSURA TOTALE se in Cash Mode)
        for symbol in list(posizioni_esistenti.keys()):
            if symbol not in tickers_alpaca and symbol != 'SPY':
                try: 
                    trading_client.close_position(symbol)
                    ordini_eseguiti.append({
                        'Ticker': symbol, 'Gamba': 'LONG', 'Azione': 'SELL (Uscita)',
                        'Prezzo ($)': 0, 'Q.tà Delta': 0, 'Controvalore ($)': 0
                    })
                except: pass

        # 2. ESECUZIONE LONG (Saltato in Cash Mode)
        for ticker in tickers_alpaca:
            prezzo = prezzi_live.get(ticker, 0)
            if prezzo <= 0: continue
            
            target_qty = target_importo_long_singolo / prezzo
            qty_attuale = posizioni_esistenti.get(ticker, {}).get('qty', 0.0)
            delta_qty = target_qty - qty_attuale
            
            if abs(delta_qty * prezzo) > (target_importo_long_singolo * 0.015):
                side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
                qty_da_trattare = round(abs(delta_qty), 4) 
                
                try:
                    req_long = MarketOrderRequest(symbol=ticker, qty=qty_da_trattare, side=side, time_in_force=TimeInForce.DAY)
                    trading_client.submit_order(order_data=req_long)
                    ordini_eseguiti.append({
                        'Ticker': ticker, 'Gamba': 'LONG', 'Azione': f"{side.name} (Rebalance)",
                        'Prezzo ($)': round(prezzo, 2), 'Q.tà Delta': qty_da_trattare,
                        'Controvalore ($)': round(qty_da_trattare * prezzo, 2)
                    })
                except: pass
            else:
                ordini_eseguiti.append({
                    'Ticker': ticker, 'Gamba': 'LONG', 'Azione': 'HOLD (Allineato)',
                    'Prezzo ($)': round(prezzo, 2), 'Q.tà Delta': 0, 'Controvalore ($)': 0
                })

        # 3. ESECUZIONE SHORT SPY (Azzerato in Cash Mode)
        prezzo_spy = prezzi_live.get('SPY', 0)
        if prezzo_spy > 0:
            target_qty_spy = int(target_importo_short_spy / prezzo_spy)
            qty_attuale_spy = posizioni_esistenti.get('SPY', {}).get('qty', 0.0) 
            delta_qty_spy = -target_qty_spy - qty_attuale_spy 
            
            if abs(delta_qty_spy) >= 1:
                side_spy = OrderSide.SELL if delta_qty_spy < 0 else OrderSide.BUY
                qty_trattata_spy = abs(int(delta_qty_spy))
                try:
                    req_spy = MarketOrderRequest(symbol='SPY', qty=qty_trattata_spy, side=side_spy, time_in_force=TimeInForce.DAY)
                    trading_client.submit_order(order_data=req_spy)
                    ordini_eseguiti.append({
                        'Ticker': 'SPY', 'Gamba': 'SHORT', 'Azione': f"{side_spy.name} (Hedge)",
                        'Prezzo ($)': round(prezzo_spy, 2), 'Q.tà Delta': qty_trattata_spy,
                        'Controvalore ($)': round(qty_trattata_spy * prezzo_spy, 2)
                    })
                except: pass
            else:
                ordini_eseguiti.append({
                    'Ticker': 'SPY', 'Gamba': 'SHORT', 'Azione': 'HOLD (Hedge Allineato)',
                    'Prezzo ($)': round(prezzo_spy, 2), 'Q.tà Delta': 0, 'Controvalore ($)': 0
                })

        return pd.DataFrame(ordini_eseguiti), df_top15
    except Exception as e:
        logger.error(f"❌ Fallimento critico: {e}")
        return None, None

def invia_email_esecutiva(df_ordini, df_top15, df_stats):
    if not SENDER_EMAIL or not RECEIVER_EMAIL: return
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = "📈 Report Quantitativo: Esecuzione e Performance"
        
        html_stats = df_stats.to_html(index=False, border=1) if df_stats is not None else "<p>Dati performance non disponibili.</p>"
        html_ordini = df_ordini.to_html(index=False, border=1) if df_ordini is not None and not df_ordini.empty else "<p>Nessun movimento necessario (Cash Mode attivo senza esposizioni residue).</p>"
        
        if df_top15 is not None and not df_top15.empty:
            col_utili = [c for c in ['Ticker', 'Settore', 'Score_Dinamico_100', 'FCF_Yield_%', 'Prezzo'] if c in df_top15.columns]
            html_top15 = df_top15[col_utili].round(2).to_html(index=False, border=1)
        else:
            html_top15 = "<p>⚠️ <b>DIFESA ATTIVA (CASH MODE):</b> Nessun titolo ha superato i test di bilancio. Portafoglio 100% Liquido.</p>"
            
        html = f"""
        <html><body>
            <h2>Dashboard Portafoglio Market-Neutral</h2>
            
            <h3>1. Performance del Conto (Live Alpaca)</h3>
            {html_stats}
            
            <br>
            <h3>2. Transazioni Odierne</h3>
            {html_ordini}
            
            <br>
            <h3>3. Paniere Top 15 Attivo</h3>
            {html_top15}
        </body></html>
        """
        msg.attach(MIMEText(html, 'html'))
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        logger.warning(f"❌ Errore Email: {e}")

if __name__ == "__main__":
    df_stats = calcola_statistiche_performance()
    df_movimenti, df_selezionati = esegui_rebalancing_market_neutral()
    invia_email_esecutiva(df_movimenti, df_selezionati, df_stats)
