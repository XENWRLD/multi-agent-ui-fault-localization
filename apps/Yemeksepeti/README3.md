# README File for Yemeksepeti Instance

The objective of this trace is to evaluate the system's ability to detect and correctly classify a visual mismatch failure within a food delivery workflow. This trace depicts a visual inconsistency error where the user intends to order a specific meal, but the final order summary reflects different item compared to the selection phase.

### Scenario Folder Structure

The Yemeksepeti scenario folder has the following structure:
1. **PNG images**: shows 15 (0-14) of the pre/post states of the application throughout the ordering flow.
2. **steps.json**: contains the ground truth, showing steps with instructions for every action.
3. **log.json**: contains the steps with the status for each step.

### Failure Identification and Cause Analysis

1. **Step 7 (Selection Mismatch):** During the item selection phase, a discrepancy occurs between the user's intended choice and the item added to the basket. Since no network errors are present, the system must visually verify the text and image of the selected product against the cart confirmation.
2. **Step 14 (Final Checkout Failure):** The workflow reaches a terminal failure at the final step. Despite the user attempting to finalize the order, the UI fails to transition to the "Order Success" screen, remaining stuck on a broken or incorrect summary state.

The root cause in this instance is classified as a **Frontend UI Mismatch**. Without log evidence, these failures represent "Silent Bugs" that are often missed by traditional automated tests. This trace specifically evaluates if the VLM can detect that the visual state of the application has diverged from the expected "Happy Path" defined in the steps.json.

