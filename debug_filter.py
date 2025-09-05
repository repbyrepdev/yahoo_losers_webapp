#!/usr/bin/env python3

# Debug script to test AI filtering logic
import sys
sys.path.append('/Users/damienadams/render_projects/yahoo_losers_webapp')

# Test the logic with known problematic stocks
test_stocks = [
    {
        'Symbol': 'HOOD',
        'AI Recommendation': '🟡 WAIT & WATCH - Uncertain outcome',  # Based on recovery_data
        'AI Score': 41,
        'AI Potential %': 32,
        'Is Buy Signal': '🟡 WAIT & WATCH - Uncertain outcome'.upper().find('BUY') != -1,  # Should be False
    },
    {
        'Symbol': 'LULU',  
        'AI Recommendation': '🟡 WAIT & WATCH - Uncertain outcome',
        'AI Score': 50,
        'AI Potential %': 40,
        'Is Buy Signal': '🟡 WAIT & WATCH - Uncertain outcome'.upper().find('BUY') != -1,  # Should be False
    },
    {
        'Symbol': 'TEST_BUY',
        'AI Recommendation': '🟢 STRONG BUY THE DIP - High recovery probability',
        'AI Score': 80, 
        'AI Potential %': 50,
        'Is Buy Signal': '🟢 STRONG BUY THE DIP - High recovery probability'.upper().find('BUY') != -1,  # Should be True
    }
]

print("=== DEBUG: AI Filtering Logic ===\n")

for stock in test_stocks:
    symbol = stock['Symbol']
    ai_recommendation = stock.get('AI Recommendation', 'AVOID')
    ai_score = stock.get('AI Score', 0)
    is_buy_signal = stock.get('Is Buy Signal', False)
    
    print(f"Stock: {symbol}")
    print(f"  AI Recommendation: '{ai_recommendation}'")
    print(f"  AI Score: {ai_score}")
    print(f"  Is Buy Signal: {is_buy_signal}")
    
    # Test current filtering logic
    should_show = (
        stock.get('Is Buy Signal', False) or 
        (ai_score >= 70 and 'AVOID' not in ai_recommendation.upper())
    )
    
    print(f"  Should show in AI Recovery Recommendations: {should_show}")
    
    # Breaking down the logic
    print(f"    - Is Buy Signal check: {stock.get('Is Buy Signal', False)}")
    print(f"    - Score ≥70: {ai_score >= 70}")  
    print(f"    - Not AVOID: {'AVOID' not in ai_recommendation.upper()}")
    print(f"    - Score ≥70 AND Not AVOID: {ai_score >= 70 and 'AVOID' not in ai_recommendation.upper()}")
    
    print()

print("\n=== EXPECTED RESULTS ===")
print("- HOOD: Should NOT show (score=41 < 70, no BUY signal)")
print("- LULU: Should NOT show (score=50 < 70, no BUY signal)")
print("- TEST_BUY: Should show (has BUY signal)")