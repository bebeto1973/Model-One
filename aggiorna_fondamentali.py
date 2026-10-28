import pandas as pd
import yfinance as yf
import time

def aggiorna_fondamentali_da_sec(filepath='Database_Quantitativo_Definitivo.xlsx'):
    print("🔄 Estrazione degli ultimi dati SEC (10-K) tramite yfinance in corso...")
    
    try:
        df = pd.read_excel(filepath)
    except FileNotFoundError:
        print(f"❌ File {filepath} non trovato.")
        return
        
    # Assicuriamoci che le colonne esistano, altrimenti le creiamo
    colonne_necessarie = [
        'Ricavi Totali (Discreto)', 
        'Utile Lordo (Discreto)', 
        'Free Cash Flow YTD (Discreto)', 
        'Stock-Based Compensation (SBC) (Discreto)'
    ]
    for col in colonne_necessarie:
        if col not in df.columns:
            df[col] = 0.0

    for index, row in df.iterrows():
        # yfinance richiede il trattino per i ticker speciali (es. BRK-B), Alpaca il punto
        ticker_sym = str(row['Ticker']).replace('.', '-') 
        
        try:
            ticker = yf.Ticker(ticker_sym)
            
            # Estrae l'ultimo Conto Economico (Income Statement) e Rendiconto Finanziario (Cash Flow)
            inc_stmt = ticker.financials
            cash_flow = ticker.cashflow
            
            if not inc_stmt.empty and not cash_flow.empty:
                # Estraiamo i dati dell'ultima colonna disponibile (l'anno fiscale più recente)
                if 'Total Revenue' in inc_stmt.index:
                    df.at[index, 'Ricavi Totali (Discreto)'] = float(inc_stmt.loc['Total Revenue'].iloc[0])
                    
                if 'Gross Profit' in inc_stmt.index:
                    df.at[index, 'Utile Lordo (Discreto)'] = float(inc_stmt.loc['Gross Profit'].iloc[0])
                    
                # Per il FCF cerchiamo la riga esatta, altrimenti usiamo Operating Cash Flow - CapEx
                if 'Free Cash Flow' in cash_flow.index:
                    df.at[index, 'Free Cash Flow YTD (Discreto)'] = float(cash_flow.loc['Free Cash Flow'].iloc[0])
                elif 'Operating Cash Flow' in cash_flow.index and 'Capital Expenditure' in cash_flow.index:
                    ocf = cash_flow.loc['Operating Cash Flow'].iloc[0]
                    capex = abs(cash_flow.loc['Capital Expenditure'].iloc[0]) # Spesso il CapEx è negativo
                    df.at[index, 'Free Cash Flow YTD (Discreto)'] = float(ocf - capex)
                    
                if 'Stock Based Compensation' in cash_flow.index:
                    df.at[index, 'Stock-Based Compensation (SBC) (Discreto)'] = float(cash_flow.loc['Stock Based Compensation'].iloc[0])
                
                print(f"✅ {ticker_sym}: Fondamentali SEC aggiornati all'ultimo 10-K.")
            else:
                print(f"⚠️ {ticker_sym}: Dati SEC non disponibili su yfinance.")
                
            # Piccola pausa per evitare che l'IP venga bloccato per troppe richieste (Rate Limiting)
            time.sleep(0.5) 
            
        except Exception as e:
            print(f"❌ Errore durante l'estrazione di {ticker_sym}: {e}")
            
    # Salvataggio finale del database aggiornato
    df.to_excel(filepath, index=False)
    print(f"💾 Salvataggio completato: {filepath} è ora allineato agli ultimi report SEC.")

if __name__ == "__main__":
    aggiorna_fondamentali_da_sec()
