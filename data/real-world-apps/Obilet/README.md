# README File for Obilet Instance

The objective of this trace is to evaluate the system's ability to detect and correctly classify a non-visual back-end failure during a bus ticket workflow. This trace depicts a network timeout failure (log-based) where a user intends to book a bus ticket from Istanbul Anatolia to Ankara for April 10th. The user filters for specific companies and selects the 22:00 AKSU trip. The goal is to select seat 12 and proceed to the passenger information entry screen.

### Scenario Folder Structure

The Obilet scenario folder has the following structure:
1. **PNG images**: shows 9 (0-8) pre/post states of the application in every step.
2. **steps.json**: contains the ground truth, showing steps with instructions for every action.
3. **log.json**: contains the steps with the status and logs.

### Failure Identification and Cause Analysis

The failure occurs at step 8 during the seat reservation confirmation. While the user interacts with the 'Confirm and Continue' button, the application fails to transition to the next state, and instead becomes stuck on a loading screen, showing a consistent state failure.

The root cause of this fault is a network gateway timeout (Status Code 504). The logs integrated into this trace provide clear evidence that the POST request to the reservation API failed to receive a response from the service. Thus, the UI thread remains blocked, and the application remains in a 'Wait' mode.

