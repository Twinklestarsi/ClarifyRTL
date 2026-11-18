`timescale 1ns / 1ps
`default_nettype none

module VX_alu_muldiv_tb;
    localparam integer DATAW = 32;
    localparam integer LANES = 4;
    localparam integer MUL_LATENCY = 3;
    localparam integer DIV_LATENCY = 8;

    logic clk = 1'b0;
    logic reset;
    logic in_valid;
    wire in_ready;
    logic [2:0] in_op;
    logic [LANES*DATAW-1:0] in_a;
    logic [LANES*DATAW-1:0] in_b;
    logic [LANES-1:0] in_tmask;
    logic [7:0] in_tag;
    logic [3:0] in_wid;
    logic [4:0] in_rd;
    logic in_wb;
    logic in_sop;
    logic in_eop;
    wire out_valid;
    logic out_ready;
    wire [LANES*DATAW-1:0] out_result;
    wire [LANES-1:0] out_tmask;
    wire [7:0] out_tag;
    wire [3:0] out_wid;
    wire [4:0] out_rd;
    wire out_wb;
    wire out_sop;
    wire out_eop;

    integer errors;
    integer checks;

    VX_alu_muldiv #(
        .DATAW(DATAW),
        .NUM_LANES(LANES),
        .MUL_LATENCY(MUL_LATENCY),
        .DIV_LATENCY(DIV_LATENCY)
    ) dut (.*);

    always #5 clk = ~clk;

    function automatic [DATAW-1:0] model_lane;
        input [2:0] op;
        input [DATAW-1:0] a;
        input [DATAW-1:0] b;
        logic signed [DATAW-1:0] sa;
        logic signed [DATAW-1:0] sb;
        logic [2*DATAW-1:0] pu;
        logic signed [2*DATAW-1:0] ps;
        logic signed [DATAW:0] ma;
        logic signed [DATAW:0] mb;
        logic signed [2*DATAW+1:0] pm;
        begin
            sa = a;
            sb = b;
            pu = a * b;
            ps = sa * sb;
            ma = {sa[DATAW-1], sa};
            mb = {1'b0, b};
            pm = ma * mb;
            case (op)
                3'd0: model_lane = pu[DATAW-1:0];
                3'd1: model_lane = ps[2*DATAW-1:DATAW];
                3'd2: model_lane = pm[2*DATAW-1:DATAW];
                3'd3: model_lane = pu[2*DATAW-1:DATAW];
                3'd4: begin
                    if (b == 0)
                        model_lane = '1;
                    else if (a == 32'h80000000 && b == 32'hffffffff)
                        model_lane = a;
                    else
                        model_lane = sa / sb;
                end
                3'd5: model_lane = (b == 0) ? '1 : a / b;
                3'd6: begin
                    if (b == 0)
                        model_lane = a;
                    else if (a == 32'h80000000 && b == 32'hffffffff)
                        model_lane = '0;
                    else
                        model_lane = sa % sb;
                end
                3'd7: model_lane = (b == 0) ? a : a % b;
            endcase
        end
    endfunction

    function automatic [LANES*DATAW-1:0] model_vector;
        input [2:0] op;
        input [LANES*DATAW-1:0] a;
        input [LANES*DATAW-1:0] b;
        integer lane;
        begin
            for (lane = 0; lane < LANES; lane = lane + 1)
                model_vector[lane*DATAW +: DATAW] = model_lane(
                    op, a[lane*DATAW +: DATAW], b[lane*DATAW +: DATAW]);
        end
    endfunction

    task automatic fail(input string message);
        begin
            $display("ERROR %0t: %s", $time, message);
            errors = errors + 1;
        end
    endtask

    task automatic issue_and_check(
        input [2:0] op,
        input [LANES*DATAW-1:0] a,
        input [LANES*DATAW-1:0] b,
        input [LANES-1:0] mask,
        input [7:0] tag,
        input integer latency
    );
        logic [LANES*DATAW-1:0] expected;
        integer k;
        begin
            expected = model_vector(op, a, b);
            @(negedge clk);
            in_op = op;
            in_a = a;
            in_b = b;
            in_tmask = mask;
            in_tag = tag;
            in_wid = tag[3:0];
            in_rd = tag[4:0];
            in_wb = tag[0];
            in_sop = tag[1];
            in_eop = tag[2];
            in_valid = 1'b1;
            while (!in_ready)
                @(negedge clk);
            @(posedge clk);
            #1;
            in_valid = 1'b0;
            for (k = 1; k < latency; k = k + 1) begin
                @(posedge clk);
                #1;
                if (out_valid)
                    fail("response asserted before specified latency");
            end
            @(posedge clk);
            #1;
            if (!out_valid)
                fail("response missing at specified latency");
            while (!out_valid) begin
                @(posedge clk);
                #1;
            end
            if (out_result !== expected) begin
                $display("  op=%0d expected=%h actual=%h", op, expected, out_result);
                fail("lane result mismatch");
            end
            if (out_tmask !== mask || out_tag !== tag)
                fail("mask or tag mismatch");
            if (out_wid !== tag[3:0] || out_rd !== tag[4:0])
                fail("wid or rd metadata mismatch");
            if (out_wb !== tag[0] || out_sop !== tag[1] || out_eop !== tag[2])
                fail("control metadata mismatch");
            checks = checks + 1;
            @(posedge clk);
            #1;
        end
    endtask

    task automatic check_backpressure;
        logic [LANES*DATAW-1:0] held_data;
        logic [7:0] held_tag;
        integer k;
        begin
            out_ready = 1'b0;
            @(negedge clk);
            in_op = 3'd0;
            in_a = {32'd9, 32'd7, 32'd5, 32'd3};
            in_b = {32'd8, 32'd6, 32'd4, 32'd2};
            in_tmask = 4'b1011;
            in_tag = 8'ha5;
            in_wid = 4'h5;
            in_rd = 5'h05;
            in_wb = 1'b1;
            in_sop = 1'b0;
            in_eop = 1'b1;
            in_valid = 1'b1;
            while (!in_ready)
                @(negedge clk);
            @(posedge clk);
            #1 in_valid = 1'b0;
            repeat (MUL_LATENCY) @(posedge clk);
            #1;
            if (!out_valid)
                fail("backpressure test response missing");
            held_data = out_result;
            held_tag = out_tag;
            for (k = 0; k < 3; k = k + 1) begin
                @(posedge clk);
                #1;
                if (!out_valid || out_result !== held_data || out_tag !== held_tag)
                    fail("response changed while stalled");
            end
            out_ready = 1'b1;
            @(posedge clk);
            #1;
            if (out_valid)
                fail("response did not retire after ready");
            checks = checks + 1;
        end
    endtask

    task automatic check_independent_paths;
        logic [LANES*DATAW-1:0] div_a;
        logic [LANES*DATAW-1:0] div_b;
        logic [LANES*DATAW-1:0] mul_a;
        logic [LANES*DATAW-1:0] mul_b;
        begin
            div_a = {32'd144, 32'd121, 32'd100, 32'd81};
            div_b = {32'd12, 32'd11, 32'd10, 32'd9};
            mul_a = {32'd8, 32'd7, 32'd6, 32'd5};
            mul_b = {32'd4, 32'd3, 32'd2, 32'd1};
            @(negedge clk);
            in_op = 3'd5;
            in_a = div_a;
            in_b = div_b;
            in_tmask = 4'b1111;
            in_tag = 8'hd0;
            in_wid = 4'hd;
            in_rd = 5'h10;
            in_wb = 1'b1;
            in_sop = 1'b1;
            in_eop = 1'b0;
            in_valid = 1'b1;
            #1;
            if (!in_ready)
                fail("divide path unexpectedly unavailable");
            @(posedge clk);
            #1 in_valid = 1'b0;

            @(negedge clk);
            in_op = 3'd0;
            in_a = mul_a;
            in_b = mul_b;
            in_tag = 8'hd1;
            in_wid = 4'he;
            in_rd = 5'h11;
            in_valid = 1'b1;
            #1;
            if (!in_ready)
                fail("multiply path did not accept beside active divide");
            @(posedge clk);
            #1 in_valid = 1'b0;
            repeat (MUL_LATENCY) @(posedge clk);
            #1;
            if (!out_valid || out_tag !== 8'hd1)
                fail("concurrent multiply did not complete first");
            if (out_result !== model_vector(3'd0, mul_a, mul_b))
                fail("concurrent multiply result mismatch");
            @(posedge clk);
            #1;
            while (!out_valid) begin
                @(posedge clk);
                #1;
            end
            if (out_tag !== 8'hd0)
                fail("concurrent divide metadata mismatch");
            if (out_result !== model_vector(3'd5, div_a, div_b))
                fail("concurrent divide result mismatch");
            @(posedge clk);
            #1;
            checks = checks + 2;
        end
    endtask

    initial begin
        errors = 0;
        checks = 0;
        reset = 1'b1;
        in_valid = 1'b0;
        in_op = '0;
        in_a = '0;
        in_b = '0;
        in_tmask = '0;
        in_tag = '0;
        in_wid = '0;
        in_rd = '0;
        in_wb = 1'b0;
        in_sop = 1'b0;
        in_eop = 1'b0;
        out_ready = 1'b1;
        repeat (3) @(posedge clk);
        @(negedge clk) reset = 1'b0;
        #1;
        checks = checks + 2;
        if (!in_ready)
            fail("multiply path was not ready after reset");
        if (out_valid)
            fail("reset failed to clear pending output");

        issue_and_check(3'd0, {32'h80000000,32'hffffffff,32'd11,32'd7},
            {32'd2,32'd3,32'd13,32'd9}, 4'b1111, 8'h11, MUL_LATENCY);
        issue_and_check(3'd1, {32'h80000000,32'hfffffffe,32'h7fffffff,32'd9},
            {32'd2,32'd7,32'd3,32'd11}, 4'b1101, 8'h22, MUL_LATENCY);
        issue_and_check(3'd2, {32'hffffffff,32'h80000000,32'd17,32'd19},
            {32'hffffffff,32'd2,32'd23,32'd29}, 4'b1011, 8'h33, MUL_LATENCY);
        issue_and_check(3'd3, {32'hffffffff,32'h80000000,32'd31,32'd37},
            {32'hffffffff,32'd4,32'd41,32'd43}, 4'b0111, 8'h44, MUL_LATENCY);
        issue_and_check(3'd4, {32'h80000000,32'hfffffff1,32'd100,32'd77},
            {32'hffffffff,32'd4,32'd9,32'd0}, 4'b1111, 8'h55, DIV_LATENCY);
        issue_and_check(3'd5, {32'hffffffff,32'd101,32'd99,32'd42},
            {32'd2,32'd10,32'd7,32'd0}, 4'b1110, 8'h66, DIV_LATENCY);
        issue_and_check(3'd6, {32'h80000000,32'hfffffff1,32'd100,32'd77},
            {32'hffffffff,32'd4,32'd9,32'd0}, 4'b1101, 8'h77, DIV_LATENCY);
        issue_and_check(3'd7, {32'hffffffff,32'd101,32'd99,32'd42},
            {32'd2,32'd10,32'd7,32'd0}, 4'b1011, 8'h88, DIV_LATENCY);
        check_independent_paths();
        check_backpressure();

        // Return to reset once more to prove in-flight/output state is discarded.
        @(negedge clk);
        reset = 1'b1;
        @(posedge clk);
        #1;
        checks = checks + 1;
        if (out_valid)
            fail("second reset did not discard output state");
        @(negedge clk);
        reset = 1'b0;

        if (errors != 0)
            $fatal(1, "VX_alu_muldiv failed with %0d errors", errors);
        $display("PASS VX_alu_muldiv: %0d checks", checks);
        $finish;
    end
endmodule

`default_nettype wire
