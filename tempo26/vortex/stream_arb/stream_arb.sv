`timescale 1ns / 1ps
`default_nettype none

// Standalone packet-aware stream arbiter.
// The implementation combines round-robin fairness, priority selection,
// packet-sticky grants, a two-entry output queue, and per-input statistics.
module VX_stream_arb #(
    parameter integer NUM_INPUTS = 4,
    parameter integer DATAW = 32,
    parameter integer COUNTW = 16,
    parameter integer STICKY = 1,
    parameter integer TIMING_BUG = 0,
    parameter integer SELW = (NUM_INPUTS > 1) ? $clog2(NUM_INPUTS) : 1
) (
    input  wire                              clk,
    input  wire                              reset,
    input  wire                              flush,
    input  wire [NUM_INPUTS-1:0]             valid_in,
    output logic [NUM_INPUTS-1:0]             ready_in,
    input  wire [NUM_INPUTS-1:0][DATAW-1:0]  data_in,
    input  wire [NUM_INPUTS-1:0]             last_in,
    input  wire [NUM_INPUTS-1:0][1:0]        priority_in,
    output wire                              valid_out,
    input  wire                              ready_out,
    output wire [DATAW-1:0]                  data_out,
    output wire                              last_out,
    output wire [SELW-1:0]                   sel_out,
    output wire [1:0]                        queue_occupancy,
    output wire                              grant_locked,
    output wire [SELW-1:0]                   round_robin_ptr,
    output logic [NUM_INPUTS-1:0][COUNTW-1:0] transfer_count,
    output logic [NUM_INPUTS-1:0][COUNTW-1:0] stall_count
);

    logic [SELW-1:0] rr_ptr_r;
    logic lock_valid_r;
    logic [SELW-1:0] lock_index_r;

    logic queue_valid0_r;
    logic queue_valid1_r;
    logic [DATAW-1:0] queue_data0_r;
    logic [DATAW-1:0] queue_data1_r;
    logic queue_last0_r;
    logic queue_last1_r;
    logic [SELW-1:0] queue_sel0_r;
    logic [SELW-1:0] queue_sel1_r;

    logic select_valid;
    logic [SELW-1:0] select_index;
    logic [1:0] select_priority;
    logic [NUM_INPUTS-1:0] request_at_priority;
    logic [NUM_INPUTS-1:0] candidate_mask;
    logic [NUM_INPUTS-1:0] grant_onehot;

    logic visible_valid_delay_r;
    logic [COUNTW-1:0] total_transfers_r;
    logic [COUNTW-1:0] total_stalls_r;
    logic [COUNTW-1:0] packet_count_r;
    logic overflow_seen_r;
    logic underflow_seen_r;

    integer p;
    integer offset;
    integer scan_index;
    integer counter_index;

    wire queue_full;
    wire queue_empty;
    wire output_fire;
    wire input_fire;
    wire queue_can_push;
    wire selected_last;

    initial begin
        if (NUM_INPUTS < 1) begin
            $fatal(1, "NUM_INPUTS must be at least one");
        end
        if (DATAW < 1) begin
            $fatal(1, "DATAW must be at least one");
        end
        if (COUNTW < 2) begin
            $fatal(1, "COUNTW must be at least two");
        end
    end

    assign queue_full = queue_valid0_r && queue_valid1_r;
    assign queue_empty = !queue_valid0_r;
    assign output_fire = queue_valid0_r && ready_out;
    assign queue_can_push = !queue_full || output_fire;

    // First determine the highest priority represented by a valid request.
    always_comb begin
        select_priority = 2'b00;
        for (p = 0; p < NUM_INPUTS; p = p + 1) begin
            if (valid_in[p] && (priority_in[p] > select_priority)) begin
                select_priority = priority_in[p];
            end
        end
    end

    // A packet lock overrides priority and round-robin selection.
    always_comb begin
        request_at_priority = '0;
        for (p = 0; p < NUM_INPUTS; p = p + 1) begin
            request_at_priority[p] = valid_in[p]
                                      && (priority_in[p] == select_priority);
        end
    end

    always_comb begin
        candidate_mask = request_at_priority;
        if (STICKY && lock_valid_r) begin
            candidate_mask = '0;
            candidate_mask[lock_index_r] = valid_in[lock_index_r];
        end
    end

    // Circular scan gives equal service to contenders of the same priority.
    always_comb begin
        select_valid = 1'b0;
        select_index = rr_ptr_r;
        for (offset = 0; offset < NUM_INPUTS; offset = offset + 1) begin
            scan_index = rr_ptr_r + offset;
            if (scan_index >= NUM_INPUTS) begin
                scan_index = scan_index - NUM_INPUTS;
            end
            if (!select_valid && candidate_mask[scan_index]) begin
                select_valid = 1'b1;
                select_index = SELW'(scan_index);
            end
        end
    end

    always_comb begin
        grant_onehot = '0;
        if (select_valid) begin
            grant_onehot[select_index] = 1'b1;
        end
    end

    // Ready is asserted only for the selected input and only with queue space.
    always_comb begin
        ready_in = '0;
        if (select_valid && queue_can_push && !flush) begin
            ready_in[select_index] = 1'b1;
        end
    end

    assign input_fire = select_valid && ready_in[select_index];
    assign selected_last = last_in[select_index];

    // Queue update handles simultaneous pop and push without a bubble.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            queue_valid0_r <= 1'b0;
            queue_valid1_r <= 1'b0;
            queue_data0_r <= '0;
            queue_data1_r <= '0;
            queue_last0_r <= 1'b0;
            queue_last1_r <= 1'b0;
            queue_sel0_r <= '0;
            queue_sel1_r <= '0;
        end else begin
            case ({input_fire, output_fire})
                2'b00: begin
                    queue_valid0_r <= queue_valid0_r;
                    queue_valid1_r <= queue_valid1_r;
                end
                2'b01: begin
                    queue_valid0_r <= queue_valid1_r;
                    queue_data0_r <= queue_data1_r;
                    queue_last0_r <= queue_last1_r;
                    queue_sel0_r <= queue_sel1_r;
                    queue_valid1_r <= 1'b0;
                end
                2'b10: begin
                    if (!queue_valid0_r) begin
                        queue_valid0_r <= 1'b1;
                        queue_data0_r <= data_in[select_index];
                        queue_last0_r <= last_in[select_index];
                        queue_sel0_r <= select_index;
                    end else begin
                        queue_valid1_r <= 1'b1;
                        queue_data1_r <= data_in[select_index];
                        queue_last1_r <= last_in[select_index];
                        queue_sel1_r <= select_index;
                    end
                end
                2'b11: begin
                    if (queue_valid1_r) begin
                        queue_valid0_r <= 1'b1;
                        queue_data0_r <= queue_data1_r;
                        queue_last0_r <= queue_last1_r;
                        queue_sel0_r <= queue_sel1_r;
                        queue_valid1_r <= 1'b1;
                        queue_data1_r <= data_in[select_index];
                        queue_last1_r <= last_in[select_index];
                        queue_sel1_r <= select_index;
                    end else begin
                        queue_valid0_r <= 1'b1;
                        queue_data0_r <= data_in[select_index];
                        queue_last0_r <= last_in[select_index];
                        queue_sel0_r <= select_index;
                        queue_valid1_r <= 1'b0;
                    end
                end
                default: begin
                    queue_valid0_r <= 1'b0;
                    queue_valid1_r <= 1'b0;
                end
            endcase
        end
    end

    // Lock a source at the first non-last beat and release at packet end.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            lock_valid_r <= 1'b0;
            lock_index_r <= '0;
        end else if (input_fire) begin
            if (STICKY && !selected_last) begin
                lock_valid_r <= 1'b1;
                lock_index_r <= select_index;
            end else begin
                lock_valid_r <= 1'b0;
                lock_index_r <= '0;
            end
        end
    end

    // Round-robin pointer advances after the final accepted beat of a packet.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            rr_ptr_r <= '0;
        end else if (input_fire && (!STICKY || selected_last)) begin
            if (select_index == SELW'(NUM_INPUTS - 1)) begin
                rr_ptr_r <= '0;
            end else begin
                rr_ptr_r <= select_index + 1'b1;
            end
        end
    end

    // Per-input accounting distinguishes accepted traffic from contention.
    always_ff @(posedge clk) begin
        if (reset) begin
            for (counter_index = 0;
                 counter_index < NUM_INPUTS;
                 counter_index = counter_index + 1) begin
                transfer_count[counter_index] <= '0;
                stall_count[counter_index] <= '0;
            end
            total_transfers_r <= '0;
            total_stalls_r <= '0;
            packet_count_r <= '0;
        end else begin
            for (counter_index = 0;
                 counter_index < NUM_INPUTS;
                 counter_index = counter_index + 1) begin
                if (valid_in[counter_index] && ready_in[counter_index]) begin
                    transfer_count[counter_index]
                        <= transfer_count[counter_index] + 1'b1;
                end
                if (valid_in[counter_index] && !ready_in[counter_index]) begin
                    stall_count[counter_index]
                        <= stall_count[counter_index] + 1'b1;
                end
            end
            if (input_fire) begin
                total_transfers_r <= total_transfers_r + 1'b1;
                if (selected_last) begin
                    packet_count_r <= packet_count_r + 1'b1;
                end
            end
            if ((|valid_in) && !input_fire) begin
                total_stalls_r <= total_stalls_r + 1'b1;
            end
        end
    end

    // Sticky diagnostic flags make protocol misuse observable in simulation.
    always_ff @(posedge clk) begin
        if (reset) begin
            overflow_seen_r <= 1'b0;
            underflow_seen_r <= 1'b0;
        end else begin
            if (select_valid && queue_full && !output_fire) begin
                overflow_seen_r <= 1'b1;
            end
            if (ready_out && queue_empty) begin
                underflow_seen_r <= 1'b1;
            end
        end
    end

    // Intentional mutation: expose queue valid one cycle late.
    always_ff @(posedge clk) begin
        if (reset || flush) begin
            visible_valid_delay_r <= 1'b0;
        end else begin
            visible_valid_delay_r <= queue_valid0_r;
        end
    end

    assign valid_out = TIMING_BUG ? visible_valid_delay_r : queue_valid0_r;
    assign data_out = queue_data0_r;
    assign last_out = queue_last0_r;
    assign sel_out = queue_sel0_r;
    assign queue_occupancy = {1'b0, queue_valid0_r}
                           + {1'b0, queue_valid1_r};
    assign grant_locked = lock_valid_r;
    assign round_robin_ptr = rr_ptr_r;

endmodule

`default_nettype wire
