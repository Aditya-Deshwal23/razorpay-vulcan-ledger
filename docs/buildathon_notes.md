
🏆 Razorpay Buildathon: Judges' Guide

Welcome to the Vulcan Ledger evaluation! We have optimized this application to perfectly hit the Track 04 Bar. Here is how to evaluate our submission:

1. Test the "Sub-30 Second" Throughput
Upload the test_batch_60_comprehensive.csv file.
Observe the UI: Notice the green progress bar and metric cards (Match Rate, Exceptions) ticking up live in real-time.
The Architecture: This speed is achieved by chunking 15+ records into a single LangGraph/Gemini prompt and executing them concurrently via asyncio.gather, completely avoiding the 1-to-1 API call bottleneck.
2. Verify the Honest Exception List
Navigate to the Review (Human-in-the-Loop) tab.
Notice that the UI explicitly isolates the Monetary Variance (e.g., -₹25.50 Variance highlighted in red) from standard text classifications.
Notice that 0.00 variances correctly display as "Status/Data mismatch" rather than math errors.
3. Verify Immutable Auditing
Navigate to the Audit tab.
Notice the clean filenames (no massive database UUID hashes exposed to the user).
Click Export Audit. The system will download a fully reconciled, mathematically proven CSV strictly named audited_test_batch_60_comprehensive.csv.
