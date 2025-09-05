#!/usr/bin/env python3

# Debug script to analyze actual live application data
import sys
import os
sys.path.append('/Users/damienadams/render_projects/yahoo_losers_webapp')

import app

print("=== DEBUGGING LIVE APPLICATION DATA ===\n")

# Simulate the main application flow to see what data is being generated
try:
    print("1. Getting losers data...")
    losers_data = app.get_yahoo_losers()
    print(f"   Found {len(losers_data)} losers")
    
    print("\n2. Getting detailed analysis...")
    details_data = app.get_detailed_analysis(losers_data[:5])  # Just first 5 for debugging
    print(f"   Got details for {len(details_data)} stocks")
    
    print("\n3. Calculating enhanced analysis (with AI)...")
    enhanced_analysis = app.calculate_enhanced_investment_analysis(losers_data[:5], details_data)
    print(f"   Enhanced analysis for {len(enhanced_analysis)} stocks")
    
    print("\n4. Filtering AI recovery potential...")
    ai_recovery_picks = app.filter_ai_recovery_potential(enhanced_analysis)
    print(f"   AI Recovery Recommendations: {len(ai_recovery_picks)} stocks")
    
    print("\n=== DETAILED ANALYSIS OF FIRST FEW STOCKS ===")
    for i, stock in enumerate(enhanced_analysis[:3]):
        symbol = stock.get('Symbol', 'UNKNOWN')
        print(f"\n--- STOCK {i+1}: {symbol} ---")
        print(f"AI Recommendation: '{stock.get('AI Recommendation', 'N/A')}'")
        print(f"AI Score: {stock.get('AI Score', 'N/A')}")
        print(f"AI Potential %: {stock.get('AI Potential %', 'N/A')}")
        print(f"Is Buy Signal: {stock.get('Is Buy Signal', 'N/A')}")
        
        # Test the filtering logic for this stock
        ai_recommendation = stock.get('AI Recommendation', 'AVOID')
        ai_score = stock.get('AI Score', 0)
        should_show = (
            stock.get('Is Buy Signal', False) or 
            (ai_score >= 70 and 'AVOID' not in ai_recommendation.upper())
        )
        print(f"Should show in AI Recovery: {should_show}")
        
        # Check if it's actually in the filtered list
        in_filtered = any(s.get('Symbol') == symbol for s in ai_recovery_picks)
        print(f"Actually in AI Recovery list: {in_filtered}")
        
        if should_show != in_filtered:
            print("❌ MISMATCH! Logic says it should show but it's not in list (or vice versa)")
    
    print("\n=== AI RECOVERY PICKS SUMMARY ===")
    for pick in ai_recovery_picks:
        symbol = pick.get('Symbol', 'UNKNOWN')
        ai_rec = pick.get('AI Recommendation', 'N/A')
        ai_score = pick.get('AI Score', 'N/A')
        is_buy = pick.get('Is Buy Signal', 'N/A')
        print(f"{symbol}: Score={ai_score}, Rec='{ai_rec}', BuySignal={is_buy}")
        
except Exception as e:
    print(f"Error during debugging: {e}")
    import traceback
    traceback.print_exc()